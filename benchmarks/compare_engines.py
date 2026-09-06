"""Compare extraction engines on the same pages.

Run this before trusting either tier on a new kind of document. It reports
speed and the measurable proxies for quality -- how many equations survived,
how much text came through, how many pages the gate rejects -- so the choice of
engine is made from evidence rather than from a README.

    python benchmarks/compare_engines.py book.pdf --pages 10 11 12
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdf_library.config import load_config  # noqa: E402
from pdf_library.engines import EngineUnavailable, get_engine  # noqa: E402
from pdf_library.quality import assess_page  # noqa: E402


def run(engine_name: str, pdf: Path, pages: list[int] | None) -> dict[str, float]:
    config = load_config()
    engine = get_engine(engine_name, config)
    ok, reason = engine.available()
    if not ok:
        raise EngineUnavailable(reason)

    started = time.perf_counter()
    result = engine.extract(pdf, pages=pages)
    elapsed = time.perf_counter() - started

    equations = display = tables = chars = 0
    bad_pages = 0
    for page in result.pages:
        quality = assess_page(page.markdown)
        equations += quality.math_count
        display += quality.display_math_count
        tables += quality.table_count
        chars += quality.char_count
        bad_pages += quality.state != "good"

    count = len(result.pages) or 1
    return {
        "pages": count,
        "seconds": round(elapsed, 2),
        "pages_per_second": round(count / elapsed, 2) if elapsed else 0.0,
        "equations": equations,
        "display_equations": display,
        "tables": tables,
        "chars": chars,
        "pages_failing_gate": bad_pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", nargs="*", type=int)
    parser.add_argument(
        "--engines", nargs="*", default=["pymupdf4llm", "marker"]
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        print(f"no such file: {args.pdf}", file=sys.stderr)
        return 1

    rows: dict[str, dict[str, float]] = {}
    for name in args.engines:
        try:
            rows[name] = run(name, args.pdf, args.pages)
        except EngineUnavailable as exc:
            print(f"{name}: unavailable ({exc})", file=sys.stderr)

    if not rows:
        return 1

    metrics = list(next(iter(rows.values())))
    width = max(len(m) for m in metrics) + 2
    header = "metric".ljust(width) + "".join(name.rjust(16) for name in rows)
    print(f"\n{args.pdf.name}  pages={args.pages or 'all'}\n")
    print(header)
    print("-" * len(header))
    for metric in metrics:
        line = metric.ljust(width)
        for name in rows:
            line += str(rows[name][metric]).rjust(16)
        print(line)

    if "pymupdf4llm" in rows and "marker" in rows:
        fast, high = rows["pymupdf4llm"], rows["marker"]
        speedup = high["seconds"] / fast["seconds"] if fast["seconds"] else 0
        print(
            f"\nMarker is {speedup:.0f}x slower and recovered "
            f"{high['display_equations'] - fast['display_equations']:+d} "
            "display equations."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
