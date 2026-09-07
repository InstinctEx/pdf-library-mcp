# Reliability and Greek OCR Quality — Implementation Spec

- **Author:** Codex
- **Date:** 2026-09-07
- **Status:** Approved (the repository owner authorized implementation on 2026-09-07)
- **Reviewers:** repository owner

## Context

The library already imports, caches, searches, and selectively upgrades mathematical PDFs. The real handwritten Greek corpus validates the extraction path, but still exposes three user-visible gaps: ambiguous repeated headings, OCR errors in prose, and mathematically contradictory definitions caused by a lost prime or similar OCR error.

The MCP server also treats imports as background jobs but does not retain enough information to report the exact import job before a document row exists, and interrupted server processes can leave a job permanently marked running. These failures reduce trust in an otherwise local-first workflow.

## Functional Requirements

- **FR-1:** The quality gate MUST flag two different right-hand sides assigned to the same mathematical symbol in one math span or arrow transition, without changing either expression.
- **FR-2:** Consecutive chunks with the same generated heading MUST receive unique display headings; section lookup by the original heading MUST still retrieve the entire group.
- **FR-3:** Greek prose repair MUST only modify non-math text, must require a Greek document, and must replace a word only when a supplied lexicon has exactly one candidate reachable through at most two configured handwriting substitutions.
- **FR-4:** Every accepted prose repair MUST be recorded in the repair result with its original and replacement text.
- **FR-5:** An import job MUST be queryable by its returned job ID from submission through completion or failure.
- **FR-6:** A server starting with queued or running jobs from an earlier process MUST mark them failed with a restart explanation.
- **FR-7:** User-supplied reprocess pages MUST be positive and within the document page count before an engine is invoked.
- **FR-8:** Document status MUST distinguish pages pending OCR from pages whose original is scanned, and MUST expose the engines that have processed the document.
- **FR-9:** Import MUST reject a PDF that exceeds a configured byte or page-count limit before extraction begins; zero means no limit.

## Non-Functional Requirements

- **NFR-1:** Deterministic quality and spell repair MUST add no network dependency or model invocation to import or repair.
- **NFR-2:** Existing public CLI and MCP tool names MUST remain usable.
- **NFR-3:** Existing databases MUST migrate in place without losing documents, pages, chunks, or search indexes.
- **NFR-4:** The full unit suite and Ruff F/E9 check MUST pass.

## Acceptance Criteria

- **AC-1 (FR-1):** Given `g(x)=a \\to g(x)=b`, when assessed, then the page has `contradictory_definition:1`; a chain `a=b, b=c` does not.
- **AC-2 (FR-2):** Given three consecutive `Βήμα 3` chunks, when indexed, then their display headings are `Βήμα 3 (1/3)` through `(3/3)` and lookup for `Βήμα 3` returns all three.
- **AC-3 (FR-3/FR-4):** Given the configured lexicon contains `παραγοντική`, when repairing `παραχουτική $x$`, then only the prose word changes and the audit records that change; ambiguous candidates do not change.
- **AC-4 (FR-5):** Given an import is submitted, when its job ID is queried before document creation, then a queued/running job record is returned.
- **AC-5 (FR-6):** Given a persisted running job from another process, when a JobRunner is created, then the job becomes failed with a restart explanation.
- **AC-6 (FR-7):** Given page zero, a negative page, or a page beyond the document, when reprocessing, then the library raises a clear `LibraryError` without invoking an engine.
- **AC-7 (FR-8):** Given a scanned document upgraded with OCR, when queried, then source-scanned pages and OCR-produced pages are both reported and no page is described as pending OCR.
- **AC-8 (FR-9):** Given an import limit below a PDF's size or page count, when imported, then it raises a clear `LibraryError` before extraction.

## Edge Cases

- **EC-1:** Empty or missing lexicons cause no correction and a clear repair report, not an exception.
- **EC-2:** Math delimiters, malformed math, and code-like LaTex MUST remain byte-identical during prose repair.
- **EC-3:** Non-consecutive equal headings MUST not be numbered as one group.
- **EC-4:** Repeated job-runner construction MUST not overwrite already completed or failed jobs.
- **EC-5:** The same document may be processed by more than one engine; status reports a unique ordered list.
- **EC-6:** A zero resource limit preserves the current unlimited local workflow.

## API Contracts

```text
JobStatus = {
  id: str, kind: str, state: "queued" | "running" | "complete" | "failed",
  progress: float, detail: str | null, error: str | null,
  document_id: str | null
}

RepairResult = {
  document_id: str, pages_changed: int, pages: list[int],
  counts: dict[str, int], changes: list[{page: int, original: str, replacement: str}],
  chunk_count: int
}
```

## Data Models

| Entity | Field | Type | Constraint |
| --- | --- | --- | --- |
| chunks | raw_heading | text | nullable; original section marker |
| jobs | document_id | text | nullable until import resolves its content hash |
| repair result | changes | JSON-like list | audit-only, never stored in page text |

## Out of Scope

- Automatic mathematical correction, including restoring a lost prime: it is unsafe without consulting the page image.
- Bundling a third-party Greek lexicon: licensing must be independently audited. The implementation accepts a configured local wordlist and ships only a small project-authored mathematical vocabulary.
- LLM or vision-based correction: this remains a future, explicit opt-in backend.
- A full job queue resilient to process restart: interrupted work is surfaced as failed rather than resumed.
