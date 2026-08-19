"""Turn a Markdown document into a print-ready HTML page.

Raw Markdown prints badly: the ``##``, the ``|`` table pipes and the backticks all
come out literally. Hebrew adds a second problem, because the page has to be
right-to-left while code and identifiers inside it stay left-to-right.

This produces one self-contained HTML file (no external CSS, no fonts to
download, works offline) with a print stylesheet: A4, sensible margins, a table
of contents, and page breaks that do not fall inside a table or a code block.

Usage::

    pip install markdown
    python scripts/make_printable.py docs/PROJECT-GUIDE.he.md
    python scripts/make_printable.py docs/*.md --out build/print

Then open the .html file in a browser and press Ctrl+P. "Save as PDF" in the
print dialog gives a PDF; there is no separate step and no extra tool.

Right-to-left is applied when the file name contains ``.he.`` or the text is
mostly Hebrew, and can be forced either way with --rtl / --ltr.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:  # pragma: no cover - dependency hint
    sys.exit("This script needs the markdown package:  pip install markdown")

HEBREW = re.compile(r"[\u0590-\u05FF]")

STYLE = """
:root {
  --ink: #111827;
  --muted: #6b7280;
  --rule: #d1d5db;
  --accent: #1d4ed8;
  --code-bg: #f3f4f6;
  --note-bg: #fffbeb;
  --note-edge: #f59e0b;
}

* { box-sizing: border-box; }

body {
  font-family: "Segoe UI", "Arial Hebrew", Arial, "Noto Sans Hebrew", sans-serif;
  font-size: 11.5pt;
  line-height: 1.65;
  color: var(--ink);
  max-width: 21cm;
  margin: 0 auto;
  padding: 1.5cm 1.2cm;
  background: #fff;
}

h1, h2, h3, h4 { line-height: 1.3; break-after: avoid; page-break-after: avoid; }
h1 { font-size: 22pt; margin: 0 0 .2em; }
h2 {
  font-size: 15pt;
  margin: 1.6em 0 .5em;
  padding-bottom: .25em;
  border-bottom: 2px solid var(--rule);
}
h3 { font-size: 12.5pt; margin: 1.2em 0 .4em; color: #1f2937; }
h4 { font-size: 11.5pt; margin: 1em 0 .3em; }

p, li { orphans: 3; widows: 3; }
ul, ol { padding-inline-start: 1.4em; }
li { margin: .25em 0; }

a { color: var(--accent); text-decoration: none; }

hr { border: 0; border-top: 1px solid var(--rule); margin: 1.6em 0; }

/* Code always reads left-to-right, whatever the page direction is. */
code, pre, kbd {
  font-family: Consolas, "Courier New", monospace;
  direction: ltr;
  unicode-bidi: embed;
  text-align: left;
}
code {
  background: var(--code-bg);
  padding: .1em .35em;
  border-radius: 3px;
  font-size: .88em;
}
pre {
  background: var(--code-bg);
  border: 1px solid var(--rule);
  border-radius: 5px;
  padding: .7em .9em;
  overflow-x: auto;
  font-size: 9.5pt;
  line-height: 1.45;
  break-inside: avoid;
  page-break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: inherit; }

table {
  border-collapse: collapse;
  width: 100%;
  margin: .9em 0;
  font-size: 10pt;
  break-inside: avoid;
  page-break-inside: avoid;
}
th, td {
  border: 1px solid var(--rule);
  padding: .45em .6em;
  vertical-align: top;
  text-align: start;
}
th { background: #f9fafb; font-weight: 600; }
tbody tr:nth-child(even) { background: #fcfcfd; }

blockquote {
  margin: 1em 0;
  padding: .6em .9em;
  background: var(--note-bg);
  border-inline-start: 4px solid var(--note-edge);
  border-radius: 4px;
  break-inside: avoid;
}
blockquote p:first-child { margin-top: 0; }
blockquote p:last-child { margin-bottom: 0; }

/* Interview answers: keep a question and its answer on one page. */
p strong:first-child { color: #111827; }

.toc {
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: .8em 1.1em;
  margin: 1.4em 0 2em;
  background: #fafafa;
  font-size: 10.5pt;
  break-inside: avoid;
}
.toc-title { font-weight: 700; margin-bottom: .4em; }
.toc ol { margin: 0; padding-inline-start: 1.3em; }
.toc ol ol { padding-inline-start: 1.1em; color: var(--muted); }
.toc a { color: var(--ink); }

.meta { color: var(--muted); font-size: 10pt; margin: 0 0 1.2em; }

@media print {
  body { padding: 0; max-width: none; font-size: 10.5pt; }
  a { color: var(--ink); }
  .toc { break-after: page; page-break-after: always; }
  h2 { break-before: auto; }
  pre, table, blockquote { break-inside: avoid; page-break-inside: avoid; }
}

@page {
  size: A4;
  margin: 1.6cm 1.4cm;
}
"""

PAGE = """<!DOCTYPE html>
<html lang="{lang}" dir="{direction}">
<meta charset="utf-8">
<title>{title}</title>
<style>{style}</style>
<h1>{title}</h1>
<p class="meta">{source}</p>
{toc}
{body}
"""


def is_rtl(path: Path, text: str) -> bool:
    """Right-to-left when the name says so, or Hebrew dominates the letters."""
    if ".he." in path.name:
        return True
    hebrew = len(HEBREW.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return hebrew > latin * 0.5


def slugify(text: str, seen: set[str]) -> str:
    base = re.sub(r"[^\w\u0590-\u05FF-]+", "-", text.strip().lower()).strip("-")
    base = base or "section"
    slug, n = base, 2
    while slug in seen:
        slug, n = f"{base}-{n}", n + 1
    seen.add(slug)
    return slug


def build_toc(body_html: str) -> tuple[str, str]:
    """Add ids to h2/h3 and return (toc_html, body_with_ids)."""
    seen: set[str] = set()
    entries: list[tuple[int, str, str]] = []

    def add_id(match: re.Match[str]) -> str:
        level = int(match.group(1))
        inner = match.group(2)
        plain = re.sub(r"<[^>]+>", "", inner)
        slug = slugify(plain, seen)
        entries.append((level, plain, slug))
        return f'<h{level} id="{slug}">{inner}</h{level}>'

    body_html = re.sub(r"<h([23])>(.*?)</h\1>", add_id, body_html, flags=re.S)

    if not entries:
        return "", body_html

    lines = ['<nav class="toc"><div class="toc-title">תוכן העניינים</div><ol>']
    depth = 2
    for level, plain, slug in entries:
        if level > depth:
            lines.append("<ol>")
        elif level < depth:
            lines.append("</ol>")
        depth = level
        lines.append(f'<li><a href="#{slug}">{html.escape(plain)}</a></li>')
    if depth == 3:
        lines.append("</ol>")
    lines.append("</ol></nav>")
    return "\n".join(lines), body_html


def convert(path: Path, out_dir: Path, force: str | None) -> Path:
    text = path.read_text(encoding="utf-8")

    # The first heading becomes the page title and is dropped from the body, so it
    # is not printed twice.
    title = path.stem
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        text = "\n".join(lines[1:])

    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "md_in_html"],
        output_format="html",
    )
    toc, body = build_toc(body)

    direction = force or ("rtl" if is_rtl(path, text) else "ltr")
    page = PAGE.format(
        lang="he" if direction == "rtl" else "en",
        direction=direction,
        title=html.escape(title),
        style=STYLE,
        source=html.escape(f"{path.as_posix()}  ·  ApprovalFlow"),
        toc=toc,
        body=body,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{path.stem}.html"
    out_path.write_text(page, encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown to print-ready HTML")
    parser.add_argument("files", nargs="+", type=Path, help="Markdown files")
    parser.add_argument(
        "--out", type=Path, default=Path("build/print"), help="output directory"
    )
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument(
        "--rtl", action="store_const", const="rtl", dest="force", help="force RTL"
    )
    direction.add_argument(
        "--ltr", action="store_const", const="ltr", dest="force", help="force LTR"
    )
    args = parser.parse_args()

    for path in args.files:
        if not path.is_file():
            print(f"skipped, not a file: {path}")
            continue
        out = convert(path, args.out, args.force)
        print(f"{path}  ->  {out}")

    print("\nOpen the HTML in a browser and press Ctrl+P.")
    print("Choose 'Save as PDF' in the print dialog if you want a PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
