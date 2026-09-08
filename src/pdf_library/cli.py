"""Command line interface.

Everything the MCP server can do is reachable here too, which makes the
library usable and debuggable without an agent in the loop.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .engines import get_engine
from .library import Library, LibraryError


def _silent(fraction: float, detail: str) -> None:
    return None


def _progress(fraction: float, detail: str) -> None:
    bar = int(fraction * 30)
    sys.stderr.write(f"\r[{'#' * bar}{'.' * (30 - bar)}] {fraction:5.0%} {detail:<28}")
    sys.stderr.flush()
    if fraction >= 1.0:
        sys.stderr.write("\n")


# ----------------------------------------------------------------------
def cmd_import(args: argparse.Namespace, library: Library) -> int:
    paths: list[Path] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.pdf")))
        else:
            paths.append(path)

    failures = 0
    for path in paths:
        try:
            reporter = _silent if args.quiet else _progress
            result = library.import_pdf(path, force=args.force, progress=reporter)
        except LibraryError as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if result.cache_hit:
            print(f"{result.filename}: cache HIT, nothing reprocessed "
                  f"({result.page_count}p, {result.equation_count} equations)")
            continue

        if library.config.extraction.auto_upgrade and result.upgrade_candidates:
            print(f"  automatically upgrading {len(result.upgrade_candidates)} page(s)")
            library.upgrade_pages(
                result.document_id, result.upgrade_candidates, progress=_progress
            )
            print("  automatically correcting scanned/OCR pages with MLX-VLM")
            library.correct_ocr_pages(result.document_id, apply=True, progress=_progress)

        print(
            f"{result.filename}: {result.page_count}p in {result.duration_s}s "
            f"[{result.kind}] engine={result.engine} "
            f"equations={result.equation_count} chunks={result.chunk_count}"
        )
        if result.scanned_pages:
            print(f"  {len(result.scanned_pages)} page(s) have no text layer")
        if result.upgrade_candidates:
            print(
                f"  {len(result.upgrade_candidates)} page(s) flagged for "
                f"'pdf-library reprocess {result.document_id[:8]}'"
            )
    return 1 if failures else 0


def cmd_list(args: argparse.Namespace, library: Library) -> int:
    documents = library.list_documents()
    if not documents:
        print("Library is empty.")
        return 0
    totals = library.stats()
    print(f"{totals['documents']} documents, {totals['pages']} pages, "
          f"{totals['chunks']} chunks, {totals['equations']} equations")
    print(f"root: {totals['root']}\n")
    for doc in documents:
        print(f"{doc['document_id'][:8]}  {doc['pages']:>5}p  "
              f"{doc['quality_tier']:<6} {doc['status']:<10} {doc['title']}")
    return 0


def cmd_search(args: argparse.Namespace, library: Library) -> int:
    results = library.search(
        args.query, document_id=args.document, limit=args.limit,
        chunk_type=args.type,
    )
    if not results:
        print("No matches.")
        return 1
    for item in results:
        pages = item["pages"]
        span = f"p{pages[0]}" if pages[0] == pages[1] else f"p{pages[0]}-{pages[1]}"
        print(f"[{item['chunk_id']}] {item['document']} {span} "
              f"{item['type']} | {item['heading'] or ''}")
        print(f"    {item['snippet']}\n")
    return 0


def cmd_status(args: argparse.Namespace, library: Library) -> int:
    data = library.status(args.document)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_pages(args: argparse.Namespace, library: Library) -> int:
    data = library.get_pages(args.document, args.pages)
    for page in data["pages"]:
        print(f"\n===== page {page['page']} =====\n")
        print(page["markdown"])
    if data["missing"]:
        print(f"\n(missing: {data['missing']})", file=sys.stderr)
    return 0


def cmd_section(args: argparse.Namespace, library: Library) -> int:
    data = library.get_section(args.document, args.section)
    print(f"# {data['heading']}  (pages {data['pages']})\n")
    print(data["content"])
    return 0


def cmd_report(args: argparse.Namespace, library: Library) -> int:
    rows = library.page_report(args.document)
    print(f"{'page':>5} {'tier':<6} {'score':>6} {'state':<8} {'math':>5} "
          f"{'chars':>6}  issues")
    for row in rows:
        if args.problems_only and row["state"] == "good":
            continue
        print(f"{row['page']:>5} {row['tier']:<6} {row['score']:>6.2f} "
              f"{row['state']:<8} {row['math']:>5} {row['chars']:>6}  "
              f"{','.join(row['issues'])}")
    return 0


def cmd_review_queue(args: argparse.Namespace, library: Library) -> int:
    data = library.ocr_review_queue(args.document, args.limit)
    if not data["pages"]:
        print("No pages need visual OCR review.")
        return 0
    for item in data["pages"]:
        reasons = "; ".join(item["reasons"]) or "OCR/source risk"
        score = "?" if item["score"] is None else f"{item['score']:.2f}"
        print(f"p{item['page']}  {item['state']:<16} quality={score}  {reasons}")
    print("Use the MCP review_ocr_page tool to compare a page image and transcript.")
    return 0


def cmd_review_mark(args: argparse.Namespace, library: Library) -> int:
    result = library.record_ocr_review(
        args.document, args.page, args.verdict, args.note
    )
    print(f"p{result['page']}: {result['verdict']}")
    if result["note"]:
        print(result["note"])
    return 0


def cmd_reprocess(args: argparse.Namespace, library: Library) -> int:
    result = library.upgrade_pages(
        library.resolve(args.document)["id"],
        pages=args.pages,
        engine_name=args.engine,
        progress=_progress,
    )
    if result.get("pages") and library.config.vision.enabled:
        result["vision"] = library.correct_ocr_pages(
            result["document_id"], pages=result["pages"], apply=True, progress=_progress
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_image(args: argparse.Namespace, library: Library) -> int:
    region = library.render_page_region(
        args.document, args.page, block=args.block, max_tokens=args.max_tokens
    )
    output = args.output or Path(
        f"page-{region.page_number:04d}"
        + (f"-block{args.block}" if region.cropped else "")
        + ".jpg"
    )
    output.write_bytes(region.data)
    print(
        f"{output}: {region.width}x{region.height}, "
        f"~{region.estimated_tokens} tokens"
        + (" (cropped)" if region.cropped else " (full page)")
    )
    if region.note:
        print(f"note: {region.note}")
    return 0


def cmd_blocks(args: argparse.Namespace, library: Library) -> int:
    blocks = library.page_blocks(args.document, args.page)
    if not blocks:
        print("No block layout recorded for this page. Reprocess with the "
              "quality engine to get one.")
        return 1
    print(f"{'block':>5} {'type':<16} {'chars':>6}  bbox")
    for item in blocks:
        box = item["bbox"]
        pretty = "-" if box is None else ", ".join(f"{v:.0f}" for v in box)
        print(f"{item['block']:>5} {item['type']:<16} {item['chars']:>6}  {pretty}")
    return 0


def cmd_repair(args: argparse.Namespace, library: Library) -> int:
    result = library.repair_document(args.document)
    if not result["pages_changed"]:
        print(result.get("detail", "nothing to repair"))
        return 0
    print(f"repaired {result['pages_changed']} page(s): {result['pages']}")
    for name, count in sorted(result["counts"].items()):
        print(f"  {name}: {count}")
    for change in result.get("changes", []):
        print(f"  p{change['page']}: {change['original']} → {change['replacement']}")
    print(f"  reindexed into {result['chunk_count']} chunks")
    return 0


def cmd_reindex(args: argparse.Namespace, library: Library) -> int:
    if args.all:
        count = library.reindex_all(progress=_progress)
        print(f"{count} chunks reindexed across the whole library")
        return 0
    if not args.document:
        print("give a document, or --all", file=sys.stderr)
        return 1
    row = library.resolve(args.document)
    count = library.reindex_document(row["id"])
    print(f"{row['filename']}: {count} chunks reindexed")
    return 0


def cmd_delete(args: argparse.Namespace, library: Library) -> int:
    row = library.resolve(args.document)
    if not args.yes:
        answer = input(f"Delete {row['filename']} from the library? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    library.delete_document(row["id"])
    print(f"Deleted {row['filename']}")
    return 0


def cmd_doctor(args: argparse.Namespace, library: Library) -> int:
    config = library.config
    checks: list[tuple[str, bool, str]] = []

    checks.append(("python", sys.version_info >= (3, 11), sys.version.split()[0]))

    import sqlite3

    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        checks.append(("sqlite fts5", True, sqlite3.sqlite_version))
    except sqlite3.OperationalError as exc:
        checks.append(("sqlite fts5", False, str(exc)))

    fast = get_engine(config.extraction.fast_engine, config)
    checks.append(("fast engine", *fast.available()))

    if config.extraction.quality_engine:
        quality = get_engine(config.extraction.quality_engine, config)
        ok, reason = quality.available()
        checks.append(("quality engine", ok, reason))
        if ok and config.extraction.quality_engine == "marker":
            # marker's model runner shells out to llama.cpp; without it the
            # engine installs cleanly and then fails on the first equation.
            import os

            runner = os.environ.get("LLAMA_CPP_BINARY") or shutil.which("llama-server")
            checks.append((
                "llama-server",
                bool(runner),
                runner or "missing - marker cannot recognise equations without it "
                "(brew install llama.cpp)",
            ))

    tesseract = shutil.which("tesseract")
    checks.append((
        "tesseract",
        bool(tesseract),
        tesseract or "not installed (only needed for third-party OCR workflows)",
    ))

    writable = True
    try:
        config.ensure_dirs()
        probe = config.root / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        writable = False
        checks.append(("library root", False, str(exc)))
    if writable:
        checks.append(("library root", True, str(config.root)))

    if library.index_is_stale() and library.list_documents():
        checks.append((
            "search index",
            False,
            "built by an older version - run 'pdf-library reindex --all'",
        ))
    totals = library.stats()
    checks.append((
        "database",
        True,
        f"{totals['documents']} documents, {totals['chunks']} chunks",
    ))

    failed = 0
    for name, ok, detail in checks:
        mark = "ok  " if ok else "FAIL"
        if not ok and name in ("quality engine", "tesseract", "llama-server"):
            mark = "--  "  # optional
        elif not ok:
            failed += 1
        print(f"[{mark}] {name:<16} {detail}")
    return 1 if failed else 0


def cmd_config(args: argparse.Namespace, library: Library) -> int:
    config = library.config
    print(f"root            {config.root}")
    print(f"database        {config.db_path}")
    print(f"fast engine     {config.extraction.fast_engine}")
    print(f"quality engine  {config.extraction.quality_engine}")
    print(f"auto upgrade    {config.extraction.auto_upgrade}")
    print(f"upgrade below   {config.extraction.upgrade_below_score}")
    print(f"max resp tokens {config.response.max_response_tokens}")
    print(f"greek lexicon   {config.repair.greek_lexicon or '(disabled)'}")
    print(f"greek hunspell  {config.repair.greek_hunspell or '(auto-discover)'}")
    print(f"max PDF bytes   {config.safety.max_pdf_bytes or '(unlimited)'}")
    print(f"max PDF pages   {config.safety.max_pdf_pages or '(unlimited)'}")
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-library",
        description="Persistent local library of mathematical PDFs.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", help="override the library root directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="import one or more PDFs (or a directory)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--force", action="store_true", help="re-extract even on a cache hit")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="list documents")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="full-text search")
    p.add_argument("query")
    p.add_argument("-d", "--document")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.add_argument("-t", "--type", help="theorem, proof, definition, ...")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("status", help="processing and quality report")
    p.add_argument("document")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("page", help="print specific pages")
    p.add_argument("document")
    p.add_argument("pages", nargs="+", type=int)
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("section", help="print a named section")
    p.add_argument("document")
    p.add_argument("section")
    p.set_defaults(func=cmd_section)

    p = sub.add_parser("report", help="per-page quality table")
    p.add_argument("document")
    p.add_argument("--problems-only", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("review-queue", help="pages awaiting visual OCR review")
    p.add_argument("document")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_review_queue)

    p = sub.add_parser("review-mark", help="record a visual OCR review verdict")
    p.add_argument("document")
    p.add_argument("page", type=int)
    p.add_argument("verdict", choices=("approved", "needs_correction", "unreadable"))
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_review_mark)

    p = sub.add_parser("reprocess", help="re-extract pages with the quality engine")
    p.add_argument("document")
    p.add_argument("--pages", nargs="*", type=int)
    p.add_argument("--engine")
    p.set_defaults(func=cmd_reprocess)

    p = sub.add_parser("image", help="render a page or one block as a JPEG")
    p.add_argument("document")
    p.add_argument("page", type=int)
    p.add_argument("-b", "--block", type=int, help="crop to this block")
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--max-tokens", type=int, default=900,
                   help="approximate token budget for the image")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("blocks", help="list the laid-out regions of a page")
    p.add_argument("document")
    p.add_argument("page", type=int)
    p.set_defaults(func=cmd_blocks)

    p = sub.add_parser(
        "repair",
        help="fix Greek function names (ημ, συν, εφ) without re-running OCR",
    )
    p.add_argument("document")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("reindex", help="rebuild chunks and search index")
    p.add_argument("document", nargs="?")
    p.add_argument("--all", action="store_true", help="reindex every document")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("delete", help="remove a document from the library")
    p.add_argument("document")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("doctor", help="check the installation")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="show effective configuration")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    if args.root:
        config = dataclasses.replace(config, root=Path(args.root).expanduser())

    with Library(config) as library:
        try:
            return args.func(args, library)
        except LibraryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
