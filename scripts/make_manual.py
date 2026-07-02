"""Generate docs/manual.pdf from docs/manual.md — run at every release.

Renders the Markdown with markdown-it-py (already present wherever the messenger
is installed, since rich depends on it) and prints to PDF with headless Edge or
Chrome, so no Pandoc or LaTeX is needed. The image repo's USER-GUIDE.pdf was
produced the same way (Chromium print-to-PDF), just by hand; this scripts it.

Usage (any Python with markdown-it-py, e.g. the messenger venv or ComfyUI's
embedded Python):

    python scripts/make_manual.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "manual.md"
OUTPUT = ROOT / "docs" / "manual.pdf"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

CSS = """
@page { size: A4; margin: 22mm 18mm; }
body { font-family: 'Segoe UI', system-ui, sans-serif; font-size: 10.5pt;
       line-height: 1.5; color: #1a1a1a; max-width: 46em; margin: 0 auto; }
h1 { font-size: 20pt; border-bottom: 2px solid #7c5cff; padding-bottom: 6px; }
h2 { font-size: 14pt; margin-top: 1.6em; border-bottom: 1px solid #ddd;
     padding-bottom: 3px; }
h3 { font-size: 11.5pt; margin-top: 1.3em; }
code { font-family: Consolas, monospace; font-size: 9.5pt;
       background: #f4f2fa; padding: 1px 4px; border-radius: 3px; }
pre { background: #f4f2fa; border: 1px solid #e0dcee; border-radius: 4px;
      padding: 8px 10px; overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; }
th { background: #f4f2fa; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #ddd; }
h2, h3 { page-break-after: avoid; }
pre, table { page-break-inside: avoid; }
"""


def find_browser() -> str:
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    sys.exit("No Edge or Chrome found for PDF printing; install one or adjust BROWSERS.")


def version_from_pyproject() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


def main() -> None:
    md_text = SOURCE.read_text(encoding="utf-8")
    version = version_from_pyproject()
    if version not in md_text[:300]:
        print(f"warning: the manual's header does not mention version {version}")

    body = MarkdownIt("commonmark").enable("table").render(md_text)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Spark Fuse Cloud GPU Bridge {version}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )

    with tempfile.TemporaryDirectory(prefix="spark-fuse-manual-") as tmp:
        html_path = Path(tmp) / "manual.html"
        html_path.write_text(html, encoding="utf-8")
        browser = find_browser()
        result = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={OUTPUT}", html_path.as_uri()],
            capture_output=True, text=True, timeout=120,
        )
        if not OUTPUT.is_file():
            sys.exit(f"PDF was not produced.\nstdout: {result.stdout}\nstderr: {result.stderr}")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes) for version {version}")


if __name__ == "__main__":
    main()
