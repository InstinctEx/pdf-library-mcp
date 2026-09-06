"""Marker's JSON blocks, and their conversion to Markdown.

Marker can render straight to Markdown, but its JSON output carries three
things the Markdown does not: a bounding box for every block, the block's type
(``Equation``, ``Text``, ``TableCell`` and so on), and how the page's text was
obtained. Those pay for themselves several times over -- they give reliable
chunk types, an honest OCR flag, and the coordinates needed to show a reader
the original of a single equation instead of a whole page.

The cost is that JSON blocks carry HTML rather than Markdown, so the
conversion happens here. Marker's HTML vocabulary is small and regular, which
is what makes this practical.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# Block types Marker emits that we care to distinguish.
EQUATION_BLOCKS = {"Equation"}
HEADING_BLOCKS = {"SectionHeader", "Title"}
TABLE_BLOCKS = {"Table", "TableCell", "TableGroup"}
FIGURE_BLOCKS = {"Figure", "Picture", "FigureGroup", "PictureGroup"}
SKIP_BLOCKS = {"PageHeader", "PageFooter"}


@dataclass
class Block:
    """One laid-out region of a page."""

    block_type: str
    markdown: str
    bbox: tuple[float, float, float, float] | None = None
    page_number: int = 0

    @property
    def is_equation(self) -> bool:
        return self.block_type in EQUATION_BLOCKS


@dataclass
class PageBlocks:
    page_number: int
    blocks: list[Block] = field(default_factory=list)
    # "surya" means the page was read by OCR; "pdftext" means it had a text layer.
    extraction_method: str | None = None
    width: float = 0.0
    height: float = 0.0

    @property
    def markdown(self) -> str:
        parts = [b.markdown.strip() for b in self.blocks if b.markdown.strip()]
        return "\n\n".join(parts)

    @property
    def ocr_used(self) -> bool:
        return (self.extraction_method or "").lower() == "surya"


class _MarkdownWriter(HTMLParser):
    """Convert one block's HTML into Markdown.

    Only the tags Marker actually emits are handled; anything else degrades to
    its text content, which is the safe direction.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._heading: int | None = None
        self._list_stack: list[str] = []
        self._item_index: list[int] = []
        self._in_table = False
        self._row: list[str] = []
        self._rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._math_display = False

    # ------------------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self._cell is not None:
            self._cell.append(text)
        else:
            self.out.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "math":
            self._math_display = attributes.get("display") == "block"
            self._emit("\n\n$$\n" if self._math_display else "$")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = int(tag[1])
            self._emit("\n\n" + "#" * self._heading + " ")
        elif tag == "p":
            self._emit("\n\n")
        elif tag == "br":
            self._emit("  \n")
        elif tag in ("b", "strong"):
            self._emit("**")
        elif tag in ("i", "em"):
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag == "sup":
            self._emit("<sup>")
        elif tag == "sub":
            self._emit("<sub>")
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._item_index.append(0)
        elif tag == "li":
            depth = max(len(self._list_stack) - 1, 0)
            if self._list_stack and self._list_stack[-1] == "ol":
                self._item_index[-1] += 1
                marker = f"{self._item_index[-1]}."
            else:
                marker = "-"
            self._emit("\n" + "  " * depth + marker + " ")
        elif tag == "table":
            self._in_table = True
            self._rows = []
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._in_table:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "math":
            self._emit("\n$$\n\n" if self._math_display else "$")
            self._math_display = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = None
            self._emit("\n")
        elif tag == "p":
            self._emit("\n")
        elif tag in ("b", "strong"):
            self._emit("**")
        elif tag in ("i", "em"):
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag == "sup":
            self._emit("</sup>")
        elif tag == "sub":
            self._emit("</sub>")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
                self._item_index.pop()
            self._emit("\n")
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._in_table:
            self._rows.append(self._row)
            self._row = []
        elif tag == "table":
            self._in_table = False
            self.out.append(_table_to_markdown(self._rows))
            self._rows = []

    def handle_data(self, data: str) -> None:
        if self._math_display or self.get_starttag_text() == "<math>":
            self._emit(data)
            return
        self._emit(data)

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n\n" + "\n".join(lines) + "\n\n"


def html_to_markdown(source: str) -> str:
    """Convert one block of Marker HTML to Markdown."""
    if not source:
        return ""
    writer = _MarkdownWriter()
    writer.feed(source)
    writer.close()
    return writer.result()


def _bbox(node: dict[str, Any]) -> tuple[float, float, float, float] | None:
    box = node.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    return None


def parse_document(
    document: dict[str, Any], page_methods: dict[int, str] | None = None
) -> list[PageBlocks]:
    """Turn Marker's JSON document into per-page blocks.

    Page ids in Marker's JSON are 0-based; everything in this project is
    1-based, so the conversion happens here and nowhere else.
    """
    page_methods = page_methods or {}
    pages: list[PageBlocks] = []

    for page_node in document.get("children") or []:
        if page_node.get("block_type") != "Page":
            continue
        page_id = _page_id(page_node.get("id"))
        box = _bbox(page_node) or (0.0, 0.0, 0.0, 0.0)
        page = PageBlocks(
            page_number=page_id + 1,
            extraction_method=page_methods.get(page_id),
            width=box[2] - box[0],
            height=box[3] - box[1],
        )
        _collect(page_node.get("children") or [], page)
        pages.append(page)

    pages.sort(key=lambda p: p.page_number)
    return pages


def _collect(nodes: list[dict[str, Any]], page: PageBlocks) -> None:
    for node in nodes:
        block_type = node.get("block_type") or "Text"
        if block_type in SKIP_BLOCKS:
            continue
        markdown = html_to_markdown(node.get("html") or "")
        children = node.get("children") or []
        if markdown:
            page.blocks.append(
                Block(
                    block_type=block_type,
                    markdown=markdown,
                    bbox=_bbox(node),
                    page_number=page.page_number,
                )
            )
        elif children:
            # A group node with no rendered html of its own: descend into it.
            _collect(children, page)


def _page_id(identifier: Any) -> int:
    """Extract the page number from an id like ``/page/3/Page/12``."""
    match = re.search(r"/page/(\d+)", str(identifier or ""))
    return int(match.group(1)) if match else 0


def page_methods_from_meta(meta: dict[str, Any]) -> dict[int, str]:
    """Map 0-based page id to how Marker read that page."""
    out: dict[int, str] = {}
    for stats in meta.get("page_stats") or []:
        page_id = stats.get("page_id")
        method = stats.get("text_extraction_method")
        if page_id is not None and method:
            out[int(page_id)] = str(method)
    return out


def unescape(text: str) -> str:
    return html_module.unescape(text)
