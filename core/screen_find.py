"""`screen_find` — locate on-screen text and buttons, in `computer` coordinates.

A vision model that guesses where a button is gets it wrong by hundreds of
pixels; OCR word boxes are exact. Plain OCR still misses light text on a solid
coloured button, so two passes run: word-level OCR over the whole screen
(normal and inverted), and a detector for solid-colour rectangles of button
size whose contents are read from an upscaled crop. All of it runs locally and
only the match list is returned. Coordinates use the declared display of the
`computer` tool, so a result can be passed straight to a click.

Requires:  `tesseract` on PATH (sudo apt install tesseract-ocr)
"""

import asyncio
import re
import shutil
import subprocess
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core import computer
from core.output import clip

TOOLS = [
    {
        "name": "screen_find",
        "description": (
            "Find on-screen text or buttons and return their click coordinates, in "
            "the same coordinate space as the `computer` tool. It reads the screen "
            "with OCR, which is far more accurate than estimating a position from a "
            "screenshot, and it also reads text on coloured buttons that plain OCR "
            "misses. Give `text` to find it (case-insensitive, may be several "
            "words), or `buttons: true` to list the button-like blocks with their "
            "labels. `region` [x1, y1, x2, y2] limits the search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to find."},
                "buttons": {"type": "boolean", "description": "List button-like blocks and their labels."},
                "region": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "[x1, y1, x2, y2] in computer coordinates.",
                },
                "limit": {"type": "integer", "description": "Max results (default 10)."},
            },
        },
    }
]

_NAMES = {"screen_find"}
_MAX_TEXT = 6000
_BUTTON_WIDTH = (40, 700)
_BUTTON_HEIGHT = (18, 120)
_MIN_FILL = 0.55
_MAX_BLOCKS = 40
_WORKERS = 4


def handles(name: str) -> bool:
    return name in _NAMES


def available() -> str | None:
    if shutil.which("tesseract") is None:
        return "tesseract is not installed (sudo apt install tesseract-ocr)"
    return None


def _normal(word: str) -> str:
    return re.sub(r"[^\w]+", "", word.lower())


def _tesseract(image, psm: int) -> list[dict]:
    """Word boxes from `tesseract ... tsv`: text, box (left, top, right, bottom), conf, line key."""
    with tempfile.TemporaryDirectory(prefix="rm-ocr-") as folder:
        path = Path(folder) / "in.png"
        image.save(path)
        out = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", str(psm), "tsv"],
            capture_output=True, text=True, timeout=90, check=False,
        ).stdout
    words = []
    for line in out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            left, top, width, height, conf = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), float(cols[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        words.append({
            "text": cols[11].strip(), "box": (left, top, left + width, top + height),
            "conf": conf, "line": (cols[2], cols[3], cols[4]),
        })
    return words


def _phrases(words: list[dict], needle: str) -> list[dict]:
    """Matches of `needle` across consecutive words of one OCR line."""
    wanted = [_normal(part) for part in needle.split() if _normal(part)]
    if not wanted:
        return []
    hits = []
    by_line: dict[tuple, list[dict]] = {}
    for word in words:
        by_line.setdefault(word["line"], []).append(word)
    for line_words in by_line.values():
        forms = [_normal(w["text"]) for w in line_words]
        for i in range(len(forms) - len(wanted) + 1):
            window = forms[i:i + len(wanted)]
            if all(want in got for want, got in zip(wanted, window, strict=True)):
                group = line_words[i:i + len(wanted)]
                box = (
                    min(w["box"][0] for w in group), min(w["box"][1] for w in group),
                    max(w["box"][2] for w in group), max(w["box"][3] for w in group),
                )
                hits.append({
                    "text": " ".join(w["text"] for w in group), "box": box,
                    "conf": min(w["conf"] for w in group), "kind": "text",
                })
    return hits


def _blocks(image) -> list[tuple[int, int, int, int]]:
    """Boxes of solid-colour rectangles of button size, in image pixels."""
    from PIL import Image

    factor = 4
    small = image.convert("RGB").reduce(factor).quantize(colors=24, method=Image.Quantize.MEDIANCUT)
    width, height = small.size
    pixels = small.tobytes()
    seen = bytearray(width * height)
    boxes = []
    for start in range(width * height):
        if seen[start]:
            continue
        color = pixels[start]
        queue = deque([start])
        seen[start] = 1
        count, x0, y0, x1, y1 = 0, width, height, 0, 0
        while queue:
            cell = queue.popleft()
            y, x = divmod(cell, width)
            count += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    index = ny * width + nx
                    if not seen[index] and pixels[index] == color:
                        seen[index] = 1
                        queue.append(index)
        box_w, box_h = (x1 - x0 + 1) * factor, (y1 - y0 + 1) * factor
        fill = count / ((x1 - x0 + 1) * (y1 - y0 + 1))
        if (
            _BUTTON_WIDTH[0] <= box_w <= _BUTTON_WIDTH[1]
            and _BUTTON_HEIGHT[0] <= box_h <= _BUTTON_HEIGHT[1]
            and fill >= _MIN_FILL
        ):
            boxes.append((x0 * factor, y0 * factor, (x1 + 1) * factor, (y1 + 1) * factor))
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    return boxes[:_MAX_BLOCKS]


def _read_block(image, box: tuple[int, int, int, int]) -> dict:
    """The label inside one block: upscaled, tried as-is and inverted."""
    from PIL import Image, ImageOps

    crop = image.crop(box).convert("L")
    scale = max(1, round(100 / max(1, crop.height)))
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    best_text, best_conf = "", 0.0
    for candidate in (crop, ImageOps.invert(crop)):
        # Pad with the block's own fill, so the OCR sees one uniform background.
        padded = ImageOps.expand(candidate, border=12, fill=candidate.tobytes()[0])
        words = _tesseract(padded, 7)
        text = " ".join(w["text"] for w in words)
        conf = sum(w["conf"] for w in words) / len(words) if words else 0.0
        if len(_normal(text)) * (1 + conf / 100) > len(_normal(best_text)) * (1 + best_conf / 100):
            best_text, best_conf = text, conf
    return {"text": best_text, "box": box, "conf": best_conf, "kind": "button"}


def _overlap(a: tuple, b: tuple) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / smaller if smaller else 0.0


def find(image, text: str | None, buttons: bool) -> list[dict]:
    """Matches in image pixels: OCR hits and block labels, merged."""
    from PIL import ImageOps

    results: list[dict] = []
    blocks = _blocks(image) if (buttons or text) else []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        block_jobs = [pool.submit(_read_block, image, box) for box in blocks]
        if text:
            gray = image.convert("L")
            passes = [pool.submit(_tesseract, img, 11) for img in (gray, ImageOps.invert(gray))]
            for job in passes:
                results += _phrases(job.result(), text)
        labelled = [job.result() for job in block_jobs]
    if buttons:
        results += [b for b in labelled if b["text"].strip()]
    elif text:
        wanted = _normal(text)
        results += [b for b in labelled if wanted and wanted in _normal(b["text"])]
    return _merge(results)


def _merge(results: list[dict]) -> list[dict]:
    """Drop duplicates; a button box wins over the text inside it."""
    ordered = sorted(results, key=lambda r: (r["kind"] != "button", -r["conf"]))
    kept: list[dict] = []
    for item in ordered:
        if not any(_overlap(item["box"], k["box"]) > 0.6 for k in kept):
            kept.append(item)
    # Buttons first (what a click usually wants), then plain text; each by position.
    kept.sort(key=lambda r: (r["kind"] != "button", r["box"][1], r["box"][0]))
    return kept


def _declared(native: tuple[int, int], box: tuple, offset: tuple[int, int]) -> tuple[int, int, int, int, int, int]:
    """(cx, cy, x1, y1, x2, y2) in computer coordinates."""
    sx, sy = computer.DISPLAY_WIDTH / native[0], computer.DISPLAY_HEIGHT / native[1]
    x1, y1 = round((box[0] + offset[0]) * sx), round((box[1] + offset[1]) * sy)
    x2, y2 = round((box[2] + offset[0]) * sx), round((box[3] + offset[1]) * sy)
    return (x1 + x2) // 2, (y1 + y2) // 2, x1, y1, x2, y2


def _run(tool_input: dict) -> str:
    text = tool_input.get("text")
    buttons = bool(tool_input.get("buttons"))
    if not text and not buttons:
        return "Error: screen_find needs `text` to find, or `buttons: true`"
    missing = available()
    if missing:
        return f"Error: screen_find cannot run: {missing}"
    image, error = computer.capture()
    if error:
        return error
    native = image.size
    offset = (0, 0)
    region = tool_input.get("region")
    if region is not None:
        if not isinstance(region, list) or len(region) != 4 or not all(isinstance(v, int) for v in region):
            return "Error: `region` must be [x1, y1, x2, y2] integers"
        sx, sy = native[0] / computer.DISPLAY_WIDTH, native[1] / computer.DISPLAY_HEIGHT
        box = (max(0, round(region[0] * sx)), max(0, round(region[1] * sy)),
               min(native[0], round(region[2] * sx)), min(native[1], round(region[3] * sy)))
        if box[2] <= box[0] or box[3] <= box[1]:
            return f"Error: empty region {region}"
        image, offset = image.crop(box), (box[0], box[1])
    limit = max(1, int(tool_input.get("limit", 10)))
    matches = find(image, str(text) if text else None, buttons and not text)
    if not matches:
        what = f"{text!r}" if text else "any button-like block"
        return f"No match for {what} on the screen. Screen is {computer.DISPLAY_WIDTH}x{computer.DISPLAY_HEIGHT}."
    lines = []
    for item in matches[:limit]:
        cx, cy, x1, y1, x2, y2 = _declared(native, item["box"], offset)
        label = item["text"].strip() or "(no text)"
        lines.append(f'{item["kind"]} "{label}" center ({cx}, {cy}) box [{x1}, {y1}, {x2}, {y2}] confidence {item["conf"]:.0f}')
    head = (
        f"{len(matches)} match(es) for {text!r}" if text else f"{len(matches)} button-like block(s)"
    )
    return clip(f"{head}; coordinates are in the computer tool's {computer.DISPLAY_WIDTH}x{computer.DISPLAY_HEIGHT} space:\n"
                + "\n".join(lines), _MAX_TEXT)


async def execute(name: str, tool_input: dict) -> str:
    if name not in _NAMES:
        return f"Error: unknown tool {name!r}"
    try:
        return await asyncio.to_thread(_run, tool_input)
    except Exception as e:
        return f"Error: screen_find failed: {e}"

