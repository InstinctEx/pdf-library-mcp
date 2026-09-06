"""Build a side-by-side proofreading page for one document.

Extraction quality is a visual judgement: the only way to know whether an
equation survived is to put the original page next to the rendered Markdown and
look. This writes a single self-contained HTML file that does exactly that --
scanned page on the left, extracted Markdown with its LaTeX typeset on the
right, one row per page.

    python tools/proofread.py <document> [-o out.html] [--dpi 110]

<document> is anything `pdf-library` accepts: an id, an id prefix, a filename
or a title.
"""

from __future__ import annotations

import argparse
import base64
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf  # noqa: E402

from pdf_library.library import Library, LibraryError  # noqa: E402

TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>{title} - proofread</title>
<script>
window.MathJax = {{
  tex: {{
    inlineMath: [['$', '$']],
    displayMath: [['$$', '$$']],
    processEscapes: true,
  }},
  options: {{ skipHtmlTags: ['script', 'noscript', 'style', 'textarea'] }},
}};
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/3.2.2/es5/tex-svg.js"></script>
<style>
  :root {{
    --bg: #f6f6f4; --card: #fff; --ink: #1a1a1a; --muted: #6b6b6b;
    --line: #e2e2de; --accent: #7a5cff; --warn: #b45309;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #17171a; --card: #202024; --ink: #ececf0; --muted: #9a9aa4;
      --line: #32323a; --accent: #a08cff; --warn: #f0a44a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }}
  header {{
    position: sticky; top: 0; z-index: 5; background: var(--card);
    border-bottom: 1px solid var(--line); padding: 14px 20px;
    display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: baseline;
  }}
  header h1 {{ margin: 0; font-size: 17px; font-weight: 650; }}
  header .meta {{ color: var(--muted); font-size: 13px; }}
  header .meta b {{ color: var(--ink); font-weight: 600; }}
  main {{ padding: 20px; display: flex; flex-direction: column; gap: 20px; }}
  .row {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden;
  }}
  .rowhead {{
    padding: 8px 14px; border-bottom: 1px solid var(--line);
    display: flex; gap: 14px; align-items: center; font-size: 13px;
    color: var(--muted);
  }}
  .rowhead .n {{ font-weight: 700; color: var(--ink); font-size: 15px; }}
  .tag {{
    font-size: 11px; letter-spacing: .04em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 999px; border: 1px solid var(--line);
  }}
  .tag.high {{ border-color: var(--accent); color: var(--accent); }}
  .tag.warn {{ border-color: var(--warn); color: var(--warn); }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; }}
  @media (max-width: 900px) {{ .pair {{ grid-template-columns: 1fr; }} }}
  .side {{ padding: 14px; min-width: 0; }}
  .side + .side {{ border-left: 1px solid var(--line); }}
  @media (max-width: 900px) {{
    .side + .side {{ border-left: none; border-top: 1px solid var(--line); }}
  }}
  .side img {{ width: 100%; border-radius: 6px; display: block; }}
  .md {{ font-size: 14px; overflow-x: auto; }}
  .md h1, .md h2, .md h3 {{ font-size: 15px; margin: 1em 0 .4em; }}
  .empty {{ color: var(--muted); font-style: italic; }}
  mjx-container[display="true"] {{ overflow-x: auto; overflow-y: hidden; }}
</style>
<header>
  <h1>{title}</h1>
  <span class="meta">
    <b>{pages}</b> pages &middot; <b>{equations}</b> equations &middot;
    engine <b>{engine}</b> &middot; tier <b>{tier}</b>
  </span>
  <span class="meta">Left: the original page. Right: what the library stored.</span>
</header>
<main>
{rows}
</main>
"""

ROW = """<section class="row">
  <div class="rowhead">
    <span class="n">page {number}</span>
    <span class="tag {tier_class}">{tier}</span>
    <span>quality {score}</span>
    <span>{equations} equations</span>
    {issues}
  </div>
  <div class="pair">
    <div class="side"><img src="data:image/jpeg;base64,{image}" alt="page {number}"></div>
    <div class="side"><div class="md">{markdown}</div></div>
  </div>
</section>"""


def render_page_image(page: pymupdf.Page, dpi: int) -> str:
    pixmap = page.get_pixmap(dpi=dpi)
    data = pixmap.tobytes("jpeg", jpg_quality=72)
    return base64.b64encode(data).decode("ascii")


def markdown_to_html(markdown: str) -> str:
    """Minimal Markdown rendering that leaves LaTeX untouched for MathJax."""
    if not markdown.strip():
        return '<p class="empty">(nothing extracted from this page)</p>'

    out: list[str] = []
    for block in markdown.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        # Display math is passed through verbatim so MathJax can find it.
        if block.startswith("$$") and block.endswith("$$"):
            out.append(f"<p>{block}</p>")
            continue
        escaped = html.escape(block, quote=False)
        if escaped.startswith("#"):
            level = len(escaped) - len(escaped.lstrip("#"))
            out.append(f"<h{min(level, 6)}>{escaped.lstrip('# ')}</h{min(level, 6)}>")
        else:
            out.append("<p>" + escaped.replace("\n", "<br>") + "</p>")
    return "\n".join(out)


def build(document_ref: str, output: Path, dpi: int) -> Path:
    with Library() as library:
        row = library.resolve(document_ref)
        store = library.store(row["id"])
        report = {page["page"]: page for page in library.page_report(row["id"])}
        pages = store.read_pages()
        totals = library.status(row["id"])

    rows: list[str] = []
    with pymupdf.open(store.source_pdf) as doc:
        for index, page in enumerate(doc, start=1):
            info = report.get(index, {})
            issues = info.get("issues") or []
            tier = info.get("tier", "?")
            rows.append(
                ROW.format(
                    number=index,
                    tier=tier,
                    tier_class="high" if tier == "high" else "warn",
                    score=f"{info.get('score', 0):.2f}",
                    equations=info.get("math", 0),
                    issues=(
                        f'<span class="tag warn">{html.escape(", ".join(issues))}</span>'
                        if issues
                        else ""
                    ),
                    image=render_page_image(page, dpi),
                    markdown=markdown_to_html(pages.get(index, "")),
                )
            )

    output.write_text(
        TEMPLATE.format(
            title=html.escape(str(row["title"] or row["filename"])),
            pages=totals["pages"],
            equations=totals["equations"],
            engine=html.escape(str(row["engine"] or "?")),
            tier=html.escape(str(row["quality_tier"])),
            rows="\n".join(rows),
        ),
        encoding="utf-8",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--dpi", type=int, default=110)
    args = parser.parse_args()

    try:
        with Library() as library:
            name = library.resolve(args.document)["filename"]
    except LibraryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output = args.output or Path(f"{Path(name).stem}-proofread.html")
    build(args.document, output, args.dpi)
    size = output.stat().st_size / 1_000_000
    print(f"wrote {output} ({size:.1f} MB) - open it in a browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
