"""Behavioural tests for core/screen_find.py.

    python test_screen_find.py

Runs `find` and the tool against a synthetic desktop drawn with a real font:
light buttons on a dark nav band, a white-on-blue button, plain text and an
icon with no text. Needs `tesseract`; without it the checks are skipped.
"""

import asyncio
import os
import shutil
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SIZE = (1920, 1080)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def desktop():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", SIZE, (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 200, 1920, 260), fill=(20, 20, 25))

    def button(box, label, fill, ink, font):
        draw.rectangle(box, fill=fill)
        w = draw.textlength(label, font=font)
        draw.text(((box[0] + box[2] - w) / 2, (box[1] + box[3]) / 2 - 11), label, font=font, fill=ink)

    bold = ImageFont.truetype(BOLD, 18)
    button((1416, 213, 1520, 247), "SIGN UP", (235, 235, 240), (20, 20, 20), bold)
    button((1530, 213, 1616, 247), "LOGIN", (235, 235, 240), (20, 20, 20), bold)
    button((350, 600, 506, 650), "Download", (0, 100, 200), (255, 255, 255), bold)
    draw.text((400, 300), "Hello world example", font=ImageFont.truetype(FONT, 24), fill=(220, 220, 220))
    draw.rectangle((900, 700, 960, 760), fill=(200, 60, 60))
    draw.text((100, 100), "please login to continue", font=ImageFont.truetype(FONT, 20), fill=(220, 220, 220))
    return image


def near(point: tuple[int, int], target: tuple[int, int], tolerance: int = 8) -> bool:
    return abs(point[0] - target[0]) <= tolerance and abs(point[1] - target[1]) <= tolerance


def center(line: str) -> tuple[int, int]:
    part = line.split("center (")[1].split(")")[0]
    x, y = part.split(", ")
    return int(x), int(y)


async def main_async(sf, computer) -> None:
    sx, sy = computer.DISPLAY_WIDTH / SIZE[0], computer.DISPLAY_HEIGHT / SIZE[1]

    def declared(x, y):
        return round(x * sx), round(y * sy)

    saved = computer.capture
    computer.capture = lambda: (desktop(), None)
    try:
        print("text on a coloured button")
        out = await sf.execute("screen_find", {"text": "Download"})
        first = out.splitlines()[1]
        check("white-on-blue label is found", first.startswith('button "Download"'), out[:200])
        check("centre is in computer coordinates", near(center(first), declared(428, 625)), first)

        print("light buttons on a dark band")
        out = await sf.execute("screen_find", {"text": "login"})
        check("case-insensitive match", 'button "LOGIN"' in out, out[:200])
        check("a button ranks before plain text with the same word",
              out.splitlines()[1].startswith('button "LOGIN"') and any(l.startswith("text ") for l in out.splitlines()[2:]), out[:300])
        check("centre of LOGIN", near(center(out.splitlines()[1]), declared(1573, 230)), out.splitlines()[1])
        out = await sf.execute("screen_find", {"text": "sign up"})
        check("two-word phrase", 'button "SIGN UP"' in out and near(center(out.splitlines()[1]), declared(1468, 230)), out[:200])

        print("plain text")
        out = await sf.execute("screen_find", {"text": "world example"})
        line = out.splitlines()[1]
        check("text outside any button is found", line.startswith("text ") and "world example" in line.lower(), out[:200])
        from PIL import ImageFont

        font = ImageFont.truetype(FONT, 24)
        span = (400 + font.getlength("Hello "), 400 + font.getlength("Hello world example"))
        check("its centre is on the text", near(center(line), declared((span[0] + span[1]) / 2, 314), 8), line)

        print("no match, region, buttons")
        out = await sf.execute("screen_find", {"text": "nonexistent label"})
        check("a miss says so", out.startswith("No match for 'nonexistent label'"), out[:120])
        x1, y1 = declared(900, 100)
        x2, y2 = declared(1700, 400)
        out = await sf.execute("screen_find", {"text": "login", "region": [x1, y1, x2, y2]})
        check("a region keeps a match inside it and maps back to full-screen coordinates",
              'button "LOGIN"' in out and near(center(out.splitlines()[1]), declared(1573, 230)), out[:200])
        out = await sf.execute("screen_find", {"text": "download", "region": [x1, y1, x2, y2]})
        check("a region excludes a match outside it", out.startswith("No match"), out[:120])
        out = await sf.execute("screen_find", {"buttons": True})
        labels = [part.split('"')[1] for part in out.splitlines()[1:] if '"' in part]
        check("buttons lists the labelled blocks", {"LOGIN", "SIGN UP", "Download"} <= set(labels), str(labels))
        check("unlabelled blocks are not listed", "(no text)" not in out, out[:300])

        print("bad calls")
        check("needs text or buttons", (await sf.execute("screen_find", {})).startswith("Error: screen_find needs"))
        check("bad limit", await sf.execute("screen_find", {"text": "a", "limit": "many"}) == "Error: `limit` must be an integer")
        check("bad region", (await sf.execute("screen_find", {"text": "a", "region": [1, 2]})).startswith("Error: `region`"))
        with mock.patch("shutil.which", return_value=None):
            out = await sf.execute("screen_find", {"text": "a"})
        check("missing tesseract is named", "tesseract is not installed" in out, out)
        computer.capture = lambda: (None, "Error: no screen")
        check("a capture error is passed through", await sf.execute("screen_find", {"text": "a"}) == "Error: no screen")
    finally:
        computer.capture = saved


def main() -> int:
    if not shutil.which("tesseract"):
        print("tesseract is not installed: checks skipped")
        return 0
    from core import computer, screen_find

    asyncio.run(main_async(screen_find, computer))
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
