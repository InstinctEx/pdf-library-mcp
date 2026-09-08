# pdf-library-mcp

**A local-first MCP server for mathematical PDFs, scanned notes, and technical documents.**

Import a PDF once, turn it into searchable Markdown, cache it permanently, and let AI clients retrieve only the pages or sections they actually need.

Built for mathematics, physics, computer science, English and Greek material, including scanned and handwritten lecture notes.

```text
PDF
 │
 ▼
Hash + inspect
 │
 ▼
Fast extraction
 │
 ├── good enough ───────────────────────────────┐
 │                                             │
 └── suspicious / scanned / math-heavy         │
                  │                            │
                  ▼                            │
             Marker OCR                        │
                  │                            │
                  ▼                            │
       optional MLX-VLM correction             │
                  │                            │
                  └──────────────┬─────────────┘
                                 ▼
                         canonical Markdown
                                 │
                         cache + index
                                 │
                                 ▼
                               MCP
```

The goal is simple:

> **Do the expensive document work once. Retrieve only what the agent needs afterward.**

A 500-page textbook should not cost 500 pages of context every time you ask a question about it.

---

## Why this exists

There are already excellent PDF extraction tools.

The missing piece is that they are mostly **extractors**, not **libraries**.

| Project | What it gives | What is still missing |
| --- | --- | --- |
| **PyMuPDF4LLM** | Very fast local Markdown extraction, layout and tables | Weak mathematical extraction, no persistent library or search |
| **Marker** | Strong LaTeX, reading order and OCR | Much slower, model-heavy, no persistent retrieval layer |
| **MinerU** | Strong formula-aware document parsing | Still primarily an extractor |
| **Generic PDF MCP servers** | Page retrieval and search | Usually not designed around mathematical documents, OCR quality or page-level correction |

`pdf-library-mcp` sits above the extraction engines.

It remembers what has already been processed, indexes the result, identifies bad pages, upgrades only those pages, and exposes the library to an AI client through MCP.

It adds:

- **Content-addressed caching**
- **Fast and high-quality extraction tiers**
- **Per-page upgrades**
- **Mathematics-aware quality checks**
- **Greek-aware search**
- **OCR repair**
- **Visual OCR review**
- **Local MLX-VLM OCR correction**
- **Page-level provenance and correction history**
- **Token-budgeted retrieval**
- **Original-page image access when OCR cannot be trusted**

The extraction engines remain replaceable.

---

# Core idea

A PDF is identified by the SHA-256 hash of its bytes.

Import the exact same PDF twice and the second import performs:

```text
no extraction
no OCR
no model inference
```

The existing cached document is returned instead.

Storage is page-based, which means one bad page can be reprocessed without touching the other 800 pages in the book.

---

# Extraction pipeline

There are now three levels of document processing.

## 1. Fast extraction

**Engine:** PyMuPDF4LLM

Designed to make a document searchable quickly.

It handles:

- prose
- headings
- tables
- basic layout
- text-layer PDFs

It is extremely fast, but mathematical PDFs expose an important weakness:

> Display equations can disappear completely.

The resulting Markdown may still look perfectly valid, which makes the failure difficult to detect by inspecting the extracted text alone.

---

## 2. High-quality extraction

**Engine:** Marker

Marker is used when a page deserves heavier processing.

It provides:

- much stronger LaTeX extraction
- OCR for scanned pages
- better mathematical layout
- better reading order
- block-level bounding boxes

Import does **not** blindly run Marker over every page.

Instead:

```text
fast extraction
      │
      ▼
 quality gate
      │
      ├── page looks good → keep fast result
      │
      └── page looks suspicious → candidate for reprocessing
```

A single page can then be upgraded with:

```bash
pdf-library reprocess real-analysis --pages 243
```

The rest of the document remains untouched.

---

## 3. Local vision OCR correction

For difficult scanned or handwritten material, Marker is not always enough.

The library sends scanned/OCR pages to a **local Vision Language Model running
through MLX-VLM** after Marker. The image is authoritative and the proposed
Markdown must pass deterministic validation before replacing the Marker text.

This is particularly useful for:

- handwritten notes
- Greek handwriting
- mathematical notation
- inverse functions
- hyperbolic functions
- superscripts and subscripts
- OCR errors that are impossible to detect from text alone

The pipeline becomes:

```text
Marker transcription
        +
original page image
        │
        ▼
     MLX-VLM
        │
        ▼
vision comparison
        │
        ▼
corrected Markdown + LaTeX
```

The vision model is instructed to **transcribe**, not solve or rewrite the mathematics.

For example, it is explicitly told to distinguish:

```text
sinh  cosh  tanh  coth  sech  csch
```

from:

```text
sin   cos   tan   cot   sec   csc
```

and preserve notation such as:

```latex
\sinh^{-1}(x)
```

rather than silently changing what appears on the page.

---

# MLX-VLM on Apple Silicon

The recommended vision backend on Apple Silicon is **MLX-VLM**.

The MCP server talks to its local OpenAI-compatible API, so the vision backend is isolated from the rest of the library.

## Install

Create a separate environment if desired:

```bash
python3 -m venv ~/.venvs/pdf-vision
source ~/.venvs/pdf-vision/bin/activate

pip install -U mlx-vlm
```

Start a local model:

```bash
python -m mlx_vlm.server \
  --model mlx-community/Qwen3-VL-8B-Instruct-4bit
```

For an Apple Silicon Mac with limited unified memory, a quantized 7B/8B vision model is a sensible starting point.

---

## Configure the library

Add to `config.toml`:

```toml
[vision]
enabled = true

base_url = "http://127.0.0.1:8080/v1"
model = "mlx-community/Qwen3-VL-8B-Instruct-4bit"

timeout_seconds = 300
render_scale = 3.0

temperature = 0.0
max_tokens = 4096

apply_min_confidence = 0.80
```

The high-resolution renderer used for vision OCR is separate from the token-budgeted renderer used when sending images through MCP.

That distinction is intentional:

```text
MCP image
→ optimized for context/token cost

Vision OCR image
→ optimized for transcription accuracy
```

---

# OCR correction safety

Vision models are useful, but they are not allowed to silently rewrite the library.

Every vision correction records:

```text
original Markdown
corrected Markdown
model
confidence
status
warnings
whether it was applied
timestamp
```

The original OCR is therefore preserved.

A correction can be generated without applying it:

```text
correct_ocr_page(
    document="notes.pdf",
    page=4,
    apply=false
)
```

or for several pages:

```text
correct_ocr_pages(
    document="notes.pdf",
    pages=[1, 2, 3, 4],
    apply=false
)
```

After inspection:

```text
correct_ocr_pages(
    document="notes.pdf",
    pages=[1, 2, 3, 4],
    apply=true
)
```

A correction is only automatically accepted when it satisfies the configured confidence threshold.

Low-confidence pages remain available for review instead.

---

# Why OCR needs visual verification

Some errors cannot be detected from the extracted text.

Consider an underbrace annotation.

A handwritten or typeset expression with:

```text
f(x)
g'(x)
```

written underneath terms may be interpreted by OCR as a fraction.

The resulting output can be perfectly valid LaTeX while representing completely different mathematics.

Text-only validation cannot reliably detect that.

The original page remains the source of truth.

---

# Visual OCR review

Scan-derived pages have a separate review workflow.

An AI client can request:

```text
ocr_review_queue
```

and then inspect one page with:

```text
review_ocr_page
```

The tool returns:

- the original page image
- the saved Markdown transcription

The client can then record:

```text
approved
needs_correction
unreadable
```

using:

```text
record_ocr_review
```

Manual visual-review verdicts are audit records.

They do not silently modify the transcription.

A new extraction that changes the page returns it to the review queue.

---

# Mathematics-aware quality detection

One of the most damaging extraction failures is also one of the least obvious:

> A display equation disappears and leaves behind a blank line.

The resulting Markdown contains no malformed LaTeX and no obvious error.

To detect this, the library inspects the **fonts used by the original PDF page**.

Pages containing TeX mathematical fonts such as:

```text
CMEX
AMS symbol fonts
OpenType mathematical fonts
```

but producing no display-math block can be flagged as:

```text
display_math_missing
```

This allows the library to identify pages that deserve high-quality reprocessing even when their extracted Markdown appears superficially valid.

---

# Greek is a first-class language

Greek technical documents introduce problems that ordinary Unicode search does not solve.

## Inflection-aware search

SQLite's `unicode61` tokenizer does not stem Greek words.

For example:

```text
μερικά κλάσματα
```

should still find:

```text
μερικών κλασμάτων
```

The index therefore performs:

- accent folding
- final-sigma normalization
- lightweight Greek stemming
- identical normalization of queries

LaTeX is removed from the search index while remaining untouched in the readable Markdown.

---

# Search that survives OCR

OCR corruption is not the same problem as grammatical inflection.

The library therefore searches in stages.

| Stage | Behaviour | Result label |
| --- | --- | --- |
| Exact | All normalized/stemmed words present | `exact` |
| Partial | At least one query word present | `partial` |
| Approximate | Character-trigram similarity | `approximate` |

Search stops at the first stage that produces useful results.

Approximate matches receive a much lower score than real lexical matches, so OCR-tolerant search can rescue a failed query without outranking correct results.

---

# Greek mathematical notation repair

Greek mathematical material frequently uses localized trigonometric names:

```text
ημ   → sine
συν  → cosine
εφ   → tangent
```

OCR creates two recurring problems.

### Character confusion

Handwriting may turn:

```text
συνx
```

into something resembling:

```text
60vx
```

### Incorrect mathematical interpretation

Even correctly recognized characters may become separate variables:

```text
ημx
```

can be emitted as:

```latex
\eta \mu x
```

which means the product:

```text
η · μ · x
```

rather than a function.

Because the set of Greek mathematical function names is small and closed, these cases can be repaired deterministically.

Repairs are applied **inside mathematics only** so ordinary Greek words are not modified.

Existing documents can be repaired without rerunning OCR:

```bash
pdf-library repair real-analysis
```

---

# Handwritten Greek OCR repair

Several deterministic OCR failures are also handled.

Examples include:

- words split across lines
- duplicated word tails after line breaks
- the Greek article `η` being recognized as Latin `h`
- known mathematical-function substitutions

Other handwriting errors are deliberately **not guessed**.

Greek handwriting frequently confuses characters such as:

```text
γ ↔ χ
η ↔ υ
σ ↔ δ
```

Those cases are better handled by:

- approximate search
- visual review
- the optional MLX-VLM correction layer

rather than aggressive automatic replacement.

---

# Lecture-note structure

Handwritten lecture notes often have no Markdown headings.

They still contain semantic structure such as:

```text
Παράδειγμα
Λύση
Περίπτωση 2
Βήμα 3
Θεώρημα 2.5
```

The chunker recognizes these markers and can use them as both:

- chunk headings
- semantic chunk types

This makes queries such as:

```text
search --type solution
```

possible even when the original notes have no formal document structure.

OCR-damaged near-matches are tolerated with safeguards against accidentally turning ordinary verbs into headings.

---

# The image escape hatch

OCR will never be perfect.

For that reason the library can expose the original page to the AI client.

Images are:

- opt-in
- never returned by ordinary search
- rendered according to a token budget
- optionally cropped to a single detected block

For example:

```text
get_page_image(page=4)
```

returns the page.

But:

```text
get_page_image(page=4, block=2)
```

can return only one equation.

That is often both cheaper and sharper than sending the whole page.

Marker's block bounding boxes make this possible.

---

# Performance

The expensive parts of the pipeline are third-party extraction and model inference.

Everything else is intentionally lightweight.

The important optimization is not shaving milliseconds from SQLite.

It is this:

> **Extraction should never happen twice unless the user explicitly asks for it.**

A cached document can be reopened and searched without running:

- PyMuPDF extraction
- Marker
- OCR
- MLX-VLM

Reindexing also does not re-extract PDFs.

Text repair modifies cached text without rerunning OCR.

---

# Installation

Requires Python 3.11.

```bash
python3.11 -m venv .venv

.venv/bin/pip install -e .

.venv/bin/pdf-library doctor
```

---

## Marker support

The high-quality extraction tier is optional.

```bash
.venv/bin/pip install -e '.[marker]'

brew install llama.cpp
```

The first Marker run may download several gigabytes of models.

`pdf-library doctor` checks that the required dependencies are available.

---

## Optional Greek spell repair

For the complete inflection-aware deterministic repair mode, install:

```bash
.venv/bin/pip install -e '.[spellcheck]'
```

Place:

```text
Greek.aff
Greek.dic
```

at:

```text
<library root>/dictionaries/Greek.*
```

or configure another path:

```toml
[repair]

greek_lexicon = "/absolute/path/to/greek-words.txt"
greek_hunspell = "/absolute/path/to/Greek"
```

Corrections are deliberately conservative.

A word is changed only when the configured repair system finds a sufficiently constrained candidate.

Mathematical spans are not modified by prose repair.

---

# Command line

```bash
# Import one document
pdf-library import ~/books/real-analysis.pdf

# Import a directory
pdf-library import ~/books/

# List documents
pdf-library list

# Search
pdf-library search "dominated convergence"

# Read specific pages
pdf-library page real-analysis 243 244

# Retrieve a section
pdf-library section real-analysis "Dominated Convergence"

# Inspect quality problems
pdf-library report real-analysis --problems-only

# Upgrade one page with Marker
pdf-library reprocess real-analysis --pages 243

# Apply deterministic repair without OCR
pdf-library repair real-analysis

# Inspect detected page blocks
pdf-library blocks real-analysis 243

# Render one block
pdf-library image real-analysis 243 --block 2

# Rebuild indexes without re-extracting
pdf-library reindex --all

# Environment and dependency checks
pdf-library doctor
```

---

# Proofreading

The repository contains:

```bash
tools/proofread.py <document>
```

It generates a self-contained HTML proofreader showing:

```text
original page | extracted Markdown
```

with LaTeX rendered.

For extraction quality, side-by-side visual comparison remains the most trustworthy test.

---

# MCP setup

## Claude Code

```bash
claude mcp add pdf-library -- /absolute/path/to/.venv/bin/pdf-library-mcp
```

---

## Claude Desktop

Add the server to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pdf-library": {
      "command": "/absolute/path/to/.venv/bin/pdf-library-mcp",
      "env": {
        "PDF_LIBRARY_ROOT": "/Users/you/Documents/pdf-library"
      }
    }
  }
}
```

Restart Claude Desktop after changing the configuration or adding new MCP tools.

---

# MCP tools

| Tool | Purpose |
| --- | --- |
| `import_pdf` | Import a PDF in the background or return an existing cached document |
| `search_library` | Search headings, pages and snippets |
| `get_pages` | Return Markdown for specific pages |
| `get_chunk` | Retrieve one semantic chunk |
| `get_section` | Retrieve the chunks belonging to a section |
| `list_documents` | List library metadata |
| `document_status` | Import status and document quality information |
| `get_page_image` | Return an original page or cropped block |
| `ocr_review_queue` | List OCR pages awaiting visual verification |
| `review_ocr_page` | Return the original image together with its transcription |
| `record_ocr_review` | Store a visual-review verdict |
| `correct_ocr_page` | Compare one OCR page with the original using the local vision model |
| `correct_ocr_pages` | Run vision OCR correction across selected pages |
| `reprocess` | Re-extract selected pages using Marker |

`import_pdf` and `reprocess` return background job IDs when appropriate.

Use:

```text
job_status(job_id)
```

to follow the operation.

---

# Example vision workflow

Suppose OCR of four handwritten mathematics pages looks suspicious.

First ask the local VLM to evaluate them without modifying anything:

```text
correct_ocr_pages(
    document="ΜΑΘΗΜΑΤΙΚΑ Ι ΜΑΘΗΜΑ 10.pdf",
    pages=[1, 2, 3, 4],
    apply=false
)
```

Inspect the proposed corrections.

Then:

```text
correct_ocr_pages(
    document="ΜΑΘΗΜΑΤΙΚΑ Ι ΜΑΘΗΜΑ 10.pdf",
    pages=[1, 2, 3, 4],
    apply=true
)
```

Accepted pages are:

1. written back as canonical Markdown
2. recorded in the correction audit history
3. rebuilt into `document.md`
4. reindexed for search

The previous transcription remains recorded.

---

# Storage

Default layout:

```text
~/Documents/pdf-library/
│
├── library.db
│
└── documents/
    └── <document_id>/
        ├── source.pdf
        ├── document.md
        ├── metadata.json
        └── pages/
            ├── 0001.md
            ├── 0002.md
            └── ...
```

SQLite stores:

- documents
- pages
- chunks
- FTS search data
- jobs
- OCR reviews
- vision correction provenance

Page files are both the caching unit and retrieval unit.

That is what makes single-page replacement possible.

---

# Scanned and handwritten documents

A page without a usable text layer is not silently treated as an empty page.

It is marked as scanned and becomes eligible for OCR/high-quality processing.

Handwritten Greek remains one of the hardest cases.

Marker often recovers mathematical structure surprisingly well, including expressions such as:

```latex
\frac{A_{2,m_2}}{(x-r_2)^{m_2}}
```

while surrounding prose can still contain character substitutions.

The local vision layer exists specifically for the cases where text-only post-processing has reached its limit.

---

# Configuration

See:

```text
config.example.toml
```

The library root can be overridden with:

```bash
PDF_LIBRARY_ROOT=/path/to/library
```

and the configuration file with:

```bash
PDF_LIBRARY_CONFIG=/path/to/config.toml
```

Nothing is hard-coded.

Changes to text normalization may invalidate existing search indexes.

Run:

```bash
pdf-library doctor
```

to detect problems and:

```bash
pdf-library reindex --all
```

to rebuild search data without reprocessing the PDFs.

---

# Tests

Run:

```bash
.venv/bin/python -m pytest tests -q
```

Fixtures include compiled LaTeX documents so mathematical extraction can be compared against known source material.

The test suite covers core guarantees such as:

- cached imports do not invoke extraction again
- digital PDFs do not unnecessarily trigger OCR
- display equations remain intact across chunks
- Greek search works across inflection
- approximate OCR search does not outrank exact search
- rendered images respect their token budget
- MCP tools respect their response budgets
- vision OCR provenance is preserved
- low-confidence vision output does not silently replace canonical text

---

# Design principles

## Local first

Documents, indexes, OCR and optional vision inference stay on the user's machine.

## Extraction is expensive. Retrieval should not be.

Run extraction once.

Reuse the result indefinitely.

## Page-level quality beats whole-document perfection

Do not spend hours rerunning an 800-page book because three pages are bad.

## The original PDF remains the source of truth

Every automated extraction layer can be wrong.

The original page image is always available for verification.

## Corrections require provenance

The system keeps the previous transcription and records how a replacement was produced.

## Deterministic fixes before generative fixes

Known problems such as Greek normalization and closed mathematical notation sets are handled deterministically where possible.

Vision models are reserved for cases where visual understanding actually adds information.

---

# Deliberately not included

This project intentionally avoids several pieces of infrastructure that are not currently necessary.

### Vector database

SQLite FTS5 with language-aware normalization already handles the intended library queries.

Semantic/vector search can be added later behind the same retrieval interface.

### Cloud OCR as a requirement

The project is designed to remain usable locally.

Vision correction can run entirely on Apple Silicon through MLX-VLM.

### Automatic LLM rewriting of every page

A vision model is not part of the mandatory import path.

It is an optional correction layer for pages that justify the additional cost.

### A third traditional extraction engine

PyMuPDF4LLM and Marker already cover the fast/high-quality extraction split.

Another extractor should only be added if benchmarks show a concrete advantage.

### Postgres, Redis, workers or a web application

This is a local tool for one person's document library.

SQLite and the filesystem are enough.

---

# Project direction

See:

```text
docs/NEXT.md
```

for planned work and design decisions.

Potential future areas include:

- automatic routing of suspicious OCR pages to the local vision model
- richer vision-model benchmarking
- additional MLX-VLM model profiles
- equation-level confidence reporting
- optional semantic retrieval
- richer document provenance inspection

---

# Licence

The source code in this repository is licensed under the MIT License.

See `LICENSE` for details and for information about licenses of optional or required extraction dependencies.

The project orchestrates external extraction engines behind interfaces so that the document-library layer remains independent of any single extractor.
