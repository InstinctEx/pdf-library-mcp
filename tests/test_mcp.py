"""Every MCP tool, plus the token discipline the server is built around."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from pdf_library import mcp_server
from pdf_library.config import Config
from pdf_library.library import Library
from pdf_library.tokens import estimate_tokens


@pytest.fixture
def server(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Point the module-level server at a throwaway library."""
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_runner", None)
    return config


def call(name: str, arguments: dict) -> str:
    result = asyncio.run(mcp_server.server.call_tool(name, arguments))
    blocks = result.content if hasattr(result, "content") else result[0]
    return "\n".join(block.text for block in blocks if hasattr(block, "text"))


@pytest.fixture
def imported(server: Config, math_pdf: Path) -> str:
    with Library(server) as library:
        return library.import_pdf(math_pdf).document_id


def test_tools_are_registered(server: Config) -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}
    assert names == {
        "import_pdf",
        "search_library",
        "get_pages",
        "get_chunk",
        "get_section",
        "get_page_image",
        "review_ocr_page",
        "ocr_review_queue",
        "record_ocr_review",
        "list_documents",
        "document_status",
        "job_status",
        "reprocess",
    }


def test_every_tool_documents_itself(server: Config) -> None:
    for tool in asyncio.run(mcp_server.server.list_tools()):
        assert tool.description and len(tool.description) > 40


def test_import_reports_a_cache_hit(server: Config, math_pdf: Path) -> None:
    with Library(server) as library:
        library.import_pdf(math_pdf)
    assert "Cache HIT" in call("import_pdf", {"path": str(math_pdf)})


def test_import_of_missing_file_is_not_an_exception(server: Config) -> None:
    assert "No file at" in call("import_pdf", {"path": "/nope/none.pdf"})


def test_search_returns_snippets_not_documents(imported: str) -> None:
    output = call("search_library", {"query": "convergence", "limit": 5})
    assert "chunk" in output
    assert estimate_tokens(output) < 1500


def test_search_miss_is_helpful(imported: str) -> None:
    assert "No matches" in call("search_library", {"query": "zzzznotpresent"})


def test_get_pages_returns_only_asked_pages(imported: str) -> None:
    output = call("get_pages", {"document": imported, "pages": [1]})
    assert "page 1" in output
    assert "page 2" not in output


def test_get_pages_labels_content_as_untrusted(imported: str) -> None:
    output = call("get_pages", {"document": imported, "pages": [1]})
    assert "untrusted data" in output


def test_get_chunk_round_trip(imported: str, server: Config) -> None:
    with Library(server) as library:
        chunk_id = library.search("convergence")[0]["chunk_id"]
    assert call("get_chunk", {"chunk_id": chunk_id})


def test_get_section(imported: str) -> None:
    assert "Convergence" in call(
        "get_section", {"document": imported, "section": "Convergence"}
    )


def test_list_documents_is_metadata_only(imported: str) -> None:
    output = call("list_documents", {})
    assert "document(s)" in output
    assert "\\int" not in output


def test_document_status(imported: str) -> None:
    output = call("document_status", {"document": imported})
    assert "status: complete" in output
    assert "equations:" in output


def test_job_status_reports_the_exact_job(server: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRunner:
        def job(self, job_id):
            return {
                "id": job_id,
                "kind": "import",
                "state": "running",
                "progress": 0.5,
                "detail": "extracting",
                "document_id": None,
                "error": None,
            }

    monkeypatch.setattr(mcp_server, "_runner", FakeRunner())
    output = call("job_status", {"job_id": "abc123"})
    assert "job abc123" in output
    assert "progress: 50%" in output


def test_reprocess_reports_a_missing_engine(imported: str) -> None:
    output = call("reprocess", {"document": imported, "engine": "nonesuch"})
    assert "not available" in output


def test_reprocess_starts_a_job_without_blocking(
    imported: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool must return at once; the extraction happens on the worker."""
    submitted: list[tuple[str, str | None]] = []

    class FakeRunner:
        def submit(self, kind, document_id, body):
            submitted.append((kind, document_id))
            return "job123"

    monkeypatch.setattr(mcp_server, "_jobs", lambda: FakeRunner())
    monkeypatch.setattr(
        mcp_server, "get_engine", lambda name, config: _AvailableEngine()
    )

    output = call("reprocess", {"document": imported, "pages": [1]})
    assert "job123" in output
    assert submitted == [("upgrade", imported)]


class _AvailableEngine:
    def available(self):
        return True, "stub"


def test_response_budget_is_enforced(
    server: Config, math_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tight = dataclasses.replace(
        server, response=dataclasses.replace(server.response, max_response_tokens=60)
    )
    with Library(server) as library:
        document_id = library.import_pdf(math_pdf).document_id
    monkeypatch.setattr(mcp_server, "_config", tight)

    output = call("get_pages", {"document": document_id, "pages": [1, 2]})
    assert "truncated" in output
    assert estimate_tokens(output) < 200


def test_page_image_is_returned_with_a_cost_caption(imported: str) -> None:
    """The caption must state the cost, so the choice to spend it is informed."""
    result = asyncio.run(
        mcp_server.server.call_tool(
            "get_page_image", {"document": imported, "page": 1, "max_tokens": 300}
        )
    )
    blocks = result.content if hasattr(result, "content") else result[0]
    kinds = [b.type for b in blocks]
    assert "image" in kinds
    caption = next(b.text for b in blocks if b.type == "text")
    assert "tokens" in caption
    assert "page 1" in caption


def test_page_image_budget_is_capped(imported: str, server: Config) -> None:
    """A caller cannot ask for an unbounded image."""
    result = asyncio.run(
        mcp_server.server.call_tool(
            "get_page_image", {"document": imported, "page": 1, "max_tokens": 999999}
        )
    )
    blocks = result.content if hasattr(result, "content") else result[0]
    caption = next(b.text for b in blocks if b.type == "text")
    reported = int(caption.split("about ")[1].split(" ")[0])
    ceiling = server.response.max_image_tokens * 2
    assert reported <= ceiling * 1.1


def test_ai_can_visually_review_and_record_an_ocr_page(
    imported: str, server: Config
) -> None:
    """The review flow supplies both source pixels and transcript, then audits it."""
    with Library(server) as library:
        library.conn.execute(
            "UPDATE pages SET source_scanned=1, verification_state='pending' "
            "WHERE document_id=? AND page_number=1",
            (imported,),
        )

    assert "p1 [pending" in call("ocr_review_queue", {"document": imported})
    review = asyncio.run(
        mcp_server.server.call_tool(
            "review_ocr_page", {"document": imported, "page": 1, "max_tokens": 300}
        )
    )
    blocks = review.content if hasattr(review, "content") else review[0]
    assert any(block.type == "image" for block in blocks)
    assert "Compare the image" in next(
        block.text for block in blocks if block.type == "text"
    )
    recorded = call(
        "record_ocr_review",
        {"document": imported, "page": 1, "verdict": "approved"},
    )
    assert "Recorded OCR review" in recorded
    assert "no pages need visual OCR review" in call(
        "ocr_review_queue", {"document": imported}
    )


def test_approximate_search_says_so(server: Config, math_pdf: Path) -> None:
    with Library(server) as library:
        library.import_pdf(math_pdf)
    # A misspelling that only the trigram index can reach.
    output = call("search_library", {"query": "convrgence"})
    assert "approximate" in output or "No matches" in output
