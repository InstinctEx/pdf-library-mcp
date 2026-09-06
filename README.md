# pdf-library-mcp

A local library for mathematical PDFs. Each document is converted to Markdown
once, cached forever, indexed for search, and exposed to Claude over MCP — so
answering a question about a 500-page textbook costs a few hundred tokens
instead of the whole book.

Built for maths, physics and CS material, in English and Greek, including
scanned and handwritten lecture notes.

```
PDF → hash → inspect → extract → quality gate → cache → index → MCP
                                       ↓
                         flag the pages worth re-doing properly
```

---

## Why this exists, and why it isn't a fork

There are several good PDF-extraction projects. This is not a replacement for
any of them — it depends on two. The problem is that each solves one part of
the job, and the part that was missing is the part that matters for daily use
with an agent.

| Project | What it gives | What was still missing |
| --- | --- | --- |
| **PyMuPDF4LLM** | Very fast, local, layout-aware Markdown with tables | No LaTeX. It silently drops display equations. No persistence, no search |
| **Marker** | Genuinely good LaTeX for equations, strong reading order, OCR via its own models | ~100× slower. Nothing is cached; every read re-runs the models |
| **MinerU** | Another strong formula-aware extractor | Same: an extractor, not a library |
| **Existing PDF MCP servers** | Search and selective page reading | Built around generic documents; mathematics is not a first-class concern |

Every one of those is an **extractor**. What a person actually needs when
reading maths with an agent is a **library**: something that remembers it
already read the book, knows which pages came out badly, and hands back three
pages instead of eight hundred.

That layer is what this project is. Concretely, it adds:

- **Content-addressed caching.** A document is identified by the SHA-256 of its
  bytes. Re-importing the same file runs no extraction, no OCR, no model.
- **Two quality tiers with a per-page upgrade path**, so a book is searchable in
  seconds and only the pages that need the slow engine ever see it.
- **A quality gate that detects silently lost mathematics** — the failure mode
  no extractor reports and no check on the output can see.
- **Greek as a first-class language**, at every layer from OCR repair to search.
- **An MCP interface built around a token budget**, where search returns
  snippets and content arrives only when asked for.

Forking any single extractor would have meant inheriting its licence and its
scope while still writing all of the above. Depending on them behind an
interface keeps each one replaceable — and keeps the GPL one at a process
boundary.

---

## Two tiers, not a choice of engine

Running a model-based extractor over an 800-page textbook takes hours. Waiting
that long before the book is usable is the wrong trade, so import and quality
are separate stages:

| Tier | Engine | Speed | Produces |
| --- | --- | --- | --- |
| `fast` | PyMuPDF4LLM | ~11 pages/second, no models | Structure, prose, tables. Superscripts become inline LaTeX. Display equations are often **lost** |
| `high` | Marker | ~0.1 pages/second on an Apple-silicon GPU | Real LaTeX for display and inline maths, better multi-column order, OCR for scans |

Measured on the LaTeX test fixture with warm models
(`benchmarks/compare_engines.py`): Marker is about **120× slower** and recovers
**every** display equation the fast tier dropped.

Import always runs the fast tier. The quality gate then marks which pages are
worth upgrading, and `reprocess` sends only those to Marker. Everything else
keeps its cached text. Upgrading one page never touches the other 842.

---

## Three findings that shaped the design

These came out of running the thing on real material, and each one changed the
code.

### 1. Lost equations are invisible in the output

The damaging failure is silent: the fast extractor drops a display equation and
leaves a blank line. Nothing in the resulting Markdown says anything is wrong —
it is perfectly well-formed text that happens to be missing the mathematics.

So the gate reads the page's **fonts** instead of its text. A page typeset with
TeX's large-operator and extensible-delimiter fonts (`CMEX`, the AMS symbol
fonts, any OpenType math font) that produces no `$$` block lost its equations,
and is flagged `display_math_missing`.

On a real LaTeX textbook this fires on nearly every page. That is the honest
answer: for that material the fast tier is a search index, and Marker is how you
read the maths.

### 2. Greek inflection destroys keyword search

SQLite's `unicode61` tokeniser does no stemming, so *μερικά κλάσματα* failed to
find *μερικών κλασμάτων* — the same phrase in a different case. Accent folding
does not help, because the endings genuinely differ.

The index therefore applies accent folding, final-sigma normalisation, and a
light Greek stemmer, with identical treatment of queries. LaTeX is stripped from
the indexed text as well, so `\int_{-\infty}^{\infty}` cannot pollute ranking
while the readable Markdown keeps it untouched.

### 3. No OCR model knows Greek mathematical notation

Greek textbooks write the trigonometric functions with Greek names: `ημ` for
sine, `συν` for cosine, `εφ` for tangent. Two things go wrong, and both are
deterministic to fix:

- The characters are misread. In handwriting the σ of `συν` looks like a 6 and
  the υν like 0v, so `συνx` is transcribed faithfully but meaninglessly as
  `60vx`.
- Even when the characters are right, they are typeset as separate variables:
  `ημx` becomes `\eta \mu x`, which renders as the product η·μ·x rather than
  sin(x).

Because the set of Greek function names is small and closed, both are repaired
deterministically — inside math spans only, so ordinary words like *ημέρα* and
*εφαρμογή* are never touched. The repair is gated on the document actually
using that notation, and `pdf-library repair` applies it to documents already on
disk without re-running OCR.

---

## Install

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pdf-library doctor
```

The quality engine is optional and large (it pulls in torch), and its model
runner needs the llama.cpp server binary — without it Marker installs cleanly
and then fails on the first equation, so `doctor` checks for both:

```bash
.venv/bin/pip install -e '.[marker]'
brew install llama.cpp
```

The first Marker run downloads several GB of models before it does any work.

---

## Command line

```bash
pdf-library import ~/books/real-analysis.pdf
pdf-library import ~/books/                    # a whole directory
pdf-library list
pdf-library search "dominated convergence"
pdf-library page real-analysis 243 244
pdf-library section real-analysis "Dominated Convergence"
pdf-library report real-analysis --problems-only
pdf-library reprocess real-analysis --pages 243
pdf-library repair real-analysis               # Greek notation, no OCR
pdf-library reindex --all
pdf-library doctor
```

`tools/proofread.py <document>` writes a self-contained HTML page with each
original page beside the extracted Markdown, LaTeX typeset — the only reliable
way to judge extraction quality is to look at it.

---

## Claude Code and Claude Desktop

```bash
claude mcp add pdf-library -- /absolute/path/to/.venv/bin/pdf-library-mcp
```

For Claude Desktop, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pdf-library": {
      "command": "/absolute/path/to/.venv/bin/pdf-library-mcp",
      "env": { "PDF_LIBRARY_ROOT": "~/Documents/pdf-library" }
    }
  }
}
```

### Tools

| Tool | Returns |
| --- | --- |
| `import_pdf` | Starts a background import, or reports a cache hit immediately |
| `search_library` | Headings, pages and snippets. Accent- and inflection-insensitive |
| `get_pages` | Markdown for named pages only |
| `get_chunk` | One chunk — a theorem, a proof, a definition |
| `get_section` | Every chunk under one heading |
| `list_documents` | The library, metadata only |
| `document_status` | Progress, quality report, pages worth upgrading |
| `reprocess` | Re-extracts named pages with Marker |

Imports never block the transport: `import_pdf` returns a job id and
`document_status` reports progress. Responses are trimmed to a configured token
budget, and content-returning tools label extracted text as untrusted data.

---

## Storage

```
~/Documents/pdf-library/
├── library.db                  SQLite: documents, pages, chunks, FTS5, jobs
└── documents/<document_id>/
    ├── source.pdf
    ├── document.md
    ├── metadata.json
    └── pages/0001.md ...
```

Page files are both the unit of caching and the unit of retrieval, which is what
makes single-page reprocessing possible.

---

## Scanned and handwritten documents

A page with no text layer produces nothing at the fast tier. It is recorded as
scanned and flagged for upgrade, not silently returned as an empty page.

On handwritten Greek lecture notes Marker recovers the mathematics well —
nested subscripts such as `\frac{A_{2,m_2}}{(x-r_2)^{m_2}}` come through intact
— while the surrounding prose keeps a steady rate of character confusions (γ
read as χ, η as υ). Stemmed search absorbs some of that; exact quotation of
OCR'd handwriting does not.

One error class deserves a warning: **underbrace annotations can be read as
fractions**. A term with `f(x)` and `g'(x)` labelled underneath it may be
extracted as a fraction with those labels as the denominator. The result is
syntactically valid LaTeX and no automated check can catch it, so read pages
with underbraces against the original.

---

## Configuration

See `config.example.toml`. Override the library root with `$PDF_LIBRARY_ROOT`,
or the config file with `$PDF_LIBRARY_CONFIG`. Nothing is hard-coded.

Changing how text is normalised invalidates existing indexes; `doctor` detects
that and `pdf-library reindex --all` rebuilds them without re-extracting any
PDF.

---

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

99 tests. Fixtures are compiled from LaTeX so the mathematics has a known ground
truth; regenerate them with `python tests/fixtures/make_fixtures.py`.

The suite covers the promises that matter: a second import runs no engine, a
digital PDF triggers no OCR, a display equation is never split across chunks,
Greek queries match across inflections, and every MCP tool answers within its
token budget.

---

## Deliberately not included

Not because they are bad ideas, but because they were not needed to make the
thing work well:

- A vector database. SQLite FTS5 with proper normalisation answers these
  queries, and semantic search can be added behind the same interface later.
- An LLM in the default pipeline. The quality gate is deterministic. An LLM
  belongs on the handful of pages that fail it, opt-in, never on all of them.
- A third extraction engine. Two tiers cover the range; a third is weight
  without a measured gain.
- Anything server-shaped: no Postgres, no queue, no web frontend. It is a local
  tool for one person's bookshelf.

---

## Licence

This project's own source is MIT — see `LICENSE`, which also documents the
copyleft terms of the extraction engines it drives. PyMuPDF is AGPL-3.0 and is a
required dependency; Marker is GPL-3.0 with a commercial-use condition, is
optional, and runs as a subprocess rather than being imported.
