"""Headless document conversion — LibreOffice (soffice) plus pandoc for markdown.

One tool, `document_convert`, with a `to` parameter rather than one tool per
format pair. Two things it does that a hand-written `soffice` command line in
the bash tool does not:

  * Profile isolation. LibreOffice locks its user profile, so a second
    concurrent invocation collides with the first. Every call here gets a
    throwaway profile via -env:UserInstallation, which is the flag that is easy
    to forget and silent when missing.
  * Markdown routing. soffice has no dependable markdown import, and markdown
    is what the model actually writes, so md sources go through pandoc — and
    md -> pdf goes md -> odt (pandoc) -> pdf (soffice), because pandoc's own
    PDF path needs a LaTeX engine installed.

Requires:  soffice (libreoffice) on PATH; pandoc only for markdown sources.
"""

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

TOOLS = [
    {
        "name": "document_convert",
        "description": (
            "Convert a document to another format with headless LibreOffice "
            "(and pandoc for markdown sources). From an office format, handles "
            "docx/odt/xlsx/pptx/html/rtf/txt/epub and any crossing between them, "
            "plus pdf output. Markdown input (.md) is the normal way to author — "
            "write the .md with the file editor, then convert it here — but from "
            "markdown the only targets are pdf, docx, odt, html, epub, rtf, and "
            "txt; xlsx and pptx are not reachable from markdown. Returns JSON "
            "with the output path, its size, and how long it took."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Path to the file to convert.",
                },
                "to": {
                    "type": "string",
                    "description": (
                        "Target format extension: pdf, docx, odt, doc, rtf, txt, "
                        "html, epub, xlsx, ods, csv, pptx, odp."
                    ),
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Directory to write the result into. Defaults to the "
                        "source file's own directory."
                    ),
                },
                "reference_doc": {
                    "type": "string",
                    "description": (
                        "Optional .docx/.odt whose styles the output should follow "
                        "(markdown sources only — passed to pandoc as "
                        "--reference-doc). Use this to match house style."
                    ),
                },
            },
            "required": ["source_path", "to"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}

# soffice guesses a filter from the bare extension, but the guess is wrong or
# ambiguous for several targets, so name the filter explicitly where it matters.
_FILTERS = {
    "docx": "docx:MS Word 2007 XML",
    "doc": "doc:MS Word 97",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
    "txt": "txt:Text (encoded):UTF8",
    "csv": "csv:Text - txt - csv (StarCalc):44,34,76",
}

# Formats pandoc can write directly from markdown. Anything else (pdf) needs a
# soffice second pass; anything spreadsheet-shaped is simply nonsense from md.
_PANDOC_TARGETS = {"docx", "odt", "html", "epub", "rtf", "txt"}
_SOFFICE_TIMEOUT = 180
_PANDOC_TIMEOUT = 120


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _run(argv: list[str], timeout: int) -> tuple[bool, str]:
    """Run a converter, returning (ok, combined output)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, f"{argv[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} timed out after {timeout}s"

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, output or f"{argv[0]} exited {proc.returncode}"
    return True, output


def _soffice(source: Path, target_ext: str, outdir: Path) -> tuple[bool, str]:
    """Convert via LibreOffice in a throwaway user profile."""
    if not shutil.which("soffice"):
        return False, (
            "soffice (LibreOffice) is not on PATH — install it with "
            "`sudo apt install libreoffice` (or libreoffice-writer / -calc)"
        )
    profile = tempfile.mkdtemp(prefix="lo-profile-")
    try:
        return _run(
            [
                "soffice",
                "--headless",
                "--norestore",
                "--nolockcheck",
                # Per-call profile: without this, a second concurrent soffice
                # silently refuses to convert because the profile is locked.
                f"-env:UserInstallation=file://{profile}",
                "--convert-to",
                _FILTERS.get(target_ext, target_ext),
                "--outdir",
                str(outdir),
                str(source),
            ],
            _SOFFICE_TIMEOUT,
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _pandoc(
    source: Path, target: Path, reference_doc: str | None
) -> tuple[bool, str]:
    if not shutil.which("pandoc"):
        return False, (
            "pandoc is not on PATH, and it is what converts markdown — install "
            "it with `sudo apt install pandoc`, or convert an .html/.odt source "
            "instead"
        )
    argv = ["pandoc", "--standalone", str(source), "-o", str(target)]
    if reference_doc:
        if not Path(reference_doc).is_file():
            return False, f"reference_doc not found: {reference_doc}"
        argv += [f"--reference-doc={reference_doc}"]
    return _run(argv, _PANDOC_TIMEOUT)


async def execute(name: str, tool_input: dict) -> str:
    if name != "document_convert":
        return _err(f"unknown document tool {name!r}")
    # soffice/pandoc are blocking subprocesses; keep them off the event loop.
    return await asyncio.to_thread(_convert, tool_input)


def _convert(tool_input: dict) -> str:
    source = Path(tool_input.get("source_path", "")).expanduser()
    target_ext = str(tool_input.get("to", "")).lower().lstrip(".")
    reference_doc = tool_input.get("reference_doc")

    if not source.is_file():
        return _err(f"no such file: {source}")
    if not target_ext:
        return _err("no target format given in 'to'")

    outdir = Path(tool_input.get("output_dir") or source.parent).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _err(f"cannot create output_dir {outdir}: {e}")

    expected = outdir / f"{source.stem}.{target_ext}"
    started = time.monotonic()
    scratch: str | None = None

    try:
        if source.suffix.lower() in _MARKDOWN_SUFFIXES:
            if target_ext in _PANDOC_TARGETS:
                ok, detail = _pandoc(source, expected, reference_doc)
                via = "pandoc"
            elif target_ext == "pdf":
                # pandoc's direct PDF writer needs a LaTeX engine; go through
                # ODT and let LibreOffice paginate instead.
                scratch = tempfile.mkdtemp(prefix="md-to-pdf-")
                bridge = Path(scratch) / f"{source.stem}.odt"
                ok, detail = _pandoc(source, bridge, reference_doc)
                if ok:
                    ok, detail = _soffice(bridge, "pdf", outdir)
                via = "pandoc+soffice"
            else:
                return _err(
                    f"cannot convert markdown to {target_ext!r}; markdown targets "
                    f"are pdf and {sorted(_PANDOC_TARGETS)}"
                )
        else:
            ok, detail = _soffice(source, target_ext, outdir)
            via = "soffice"
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    if not ok:
        return _err(detail)
    if not expected.is_file():
        return _err(
            f"converter reported success but {expected} was not written. "
            f"Converter said: {detail or '(nothing)'}"
        )

    return json.dumps(
        {
            # Absolute, so the path can be handed to another tool unchanged.
            "output_path": str(expected.resolve()),
            "bytes": expected.stat().st_size,
            "seconds": round(time.monotonic() - started, 2),
            "via": via,
        }
    )
