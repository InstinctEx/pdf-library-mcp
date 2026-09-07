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
  snippets and content arrives only when asked for — including an image of the
  original page, priced and opt-in, for when the text cannot be trusted.

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

### 3. Search has to survive the OCR, not assume it

Two separate problems, two separate answers. Greek inflection is regular, so a
stemmer handles it. OCR damage is not: *παραγοντική* read as *παραχουτική*
differs in two places at once, and no rule recovers that.

So search widens in three stages, and stops at the first that finds anything:

| Stage | Matches | Reported as |
| --- | --- | --- |
| exact | every word present, after folding and stemming | `exact` |
| partial | any word present | `partial` |
| approximate | character trigrams overlap | `approximate` |

The trigram index is scored an order of magnitude lower than real word
matches, so it can never outrank them — it only exists for the case where
nothing else found anything. Results carry the stage that produced them,
because an approximate match deserves to be read as one.

### 4. No OCR model knows Greek mathematical notation

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

### 5. The errors worth fixing are the ones that change the meaning

Reading three OCR'd pages against their originals turned up four kinds of
mis-parse, and they do not all deserve the same treatment.

Two are mechanical and are repaired outright. Words broken across a line come
back either split (`παραγο-` / `-ντική`) or already joined with the tail
emitted a second time (`ολοκλήρωμα` / `- ρωμα`), and the two shapes need
opposite handling. And the Greek article η is a single letter, so OCR reads it
as a Latin `h` and, because it stands alone, files it as a mathematical
variable: `η δυσκολία` becomes `$h$ δυσκολία`.

One is only reported, deliberately. An underbrace annotation — a term with
`f(x)` and `g'(x)` written underneath — is extracted as a *fraction* over those
labels. The output is valid LaTeX that means something else entirely, and no
check on the text alone can see it. But `\frac{g'(x)}{g(x)}` is also a perfectly
ordinary logarithmic derivative, so removing it automatically would break real
mathematics. Pages are flagged `underbrace_as_fraction` instead, and the reader
is pointed at the image.

The fourth is not fixable and is not pretended otherwise: OCR of handwritten
Greek confuses γ with χ, η with υ, σ with δ. That is what the trigram index and
the image are for.

### 6. Lecture notes have structure, just not Markdown structure

Handwritten notes contain no headings, so size-based chunking produced a dozen
untitled fragments. But the structure is there in the words: *Παράδειγμα*,
*Λύση*, *Περίπτωση 2*, *Βήμα 3*, *Θεώρημα 2.5*. Those are recognised on their
folded stems and become both the chunk heading and its type, which makes
`search --type solution` and `get_section` work on material that has no
headings at all.

OCR damages those words too — *Λύση* arrives as *Λύψ*, *Εφαρμογή* as
*Εφαρμόχή* — and an exact match loses the heading on exactly the pages that
need one most, so near-matches are accepted. That tolerance has to be paid for:
it would otherwise promote *Εφαρμόζουμε*, the verb built on the same root as
the heading *Εφαρμογή*. Two guards keep it honest — a marker must begin with a
capital, and must not end in a verb ending.

---

## The image escape hatch

Extraction of handwriting will never be perfect, and no amount of repair
changes that. So there is one tool that shows the reader the original — and it
is the only expensive thing here, which is why it is opt-in and priced.

Measured on a page of handwritten Greek notes:

| | tokens | vs. the page's text |
| --- | --- | --- |
| The page's extracted text | 364 | — |
| Full page image, 150 dpi | 2318 | 6.4× |
| **One equation, cropped** | **385** | **1.1×** |

Cropping is not just cheaper — at the same budget the equation is rendered
1510×448 instead of 692×977, so it is both cheaper *and* sharper than the page
that contains it. Because Marker's JSON output gives a bounding box for every
block, `get_pages` tells the reader which blocks on an OCR'd page are equations,
and `get_page_image(page=4, block=2)` shows exactly that one.

Images are never returned by any other tool, never automatically, and the
render scale is derived from a token budget rather than a DPI, so asking for
"about 400 tokens" gets the largest image that fits.

### Visual OCR review

For scan-derived pages, the library now keeps a separate visual-review queue.
A vision-capable AI calls `ocr_review_queue`, then `review_ocr_page` for one
page. The latter returns the original page image and saved Markdown together,
so the AI can compare symbols, numbers, formulas and prose before recording
`approved`, `needs_correction`, or `unreadable` through
`record_ocr_review`. A verdict is an audit record only: it cannot silently
rewrite the document. Any new extraction that changes a scanned page returns
that page to the review queue.

## On speed

Both tiers are bounded by third-party model inference, and the rest was
measured rather than assumed:

- The fast tier runs at ~27 pages/second, of which 96% is PyMuPDF's ONNX layout
  model. It cannot be switched off — pymupdf4llm requires it — so that is the
  floor.
- Marker runs at ~0.1 pages/second. A `reprocess` of many pages is already a
  single invocation, so the model load is paid once rather than per page.
- Everything else is noise: opening the library and running a search costs
  0.55 ms, so the MCP server's per-call setup is not worth caching.

The real speed feature is that none of this happens twice. A re-import is a
cache hit in about a millisecond, reindexing never re-extracts, and `repair`
fixes stored text without touching OCR.

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

### Optional deterministic Greek prose repair

`pdf-library repair` can also correct known handwritten-OCR substitutions in
ordinary Greek prose. For the full inflection-aware mode, install `spylls` and
put `Greek.aff` plus `Greek.dic` at `<library root>/dictionaries/Greek.*`.
Point `greek_hunspell` elsewhere only when you need a custom location. A plain
UTF-8 wordlist remains available as a smaller fallback:

```bash
.venv/bin/pip install -e '.[spellcheck]'
```

```toml
[repair]
greek_lexicon = "/absolute/path/to/greek-words.txt"
greek_hunspell = "/absolute/path/to/Greek"
```

It changes a word only when exactly one word in that list is reachable through
the documented Greek handwriting confusions; it never changes text inside math
delimiters. Every accepted correction is printed as an audit entry.

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
pdf-library blocks real-analysis 243           # laid-out regions of a page
pdf-library image real-analysis 243 --block 2  # crop one region to a JPEG
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

`import_pdf` and `reprocess` return a job id. Poll `job_status(job_id)` until
it returns the document id, then use `document_status` for the quality report.

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
| `get_page_image` | The original page, or one cropped block, as an image. Opt-in and priced |
| `ocr_review_queue` | Scan/OCR pages that need image-to-text verification |
| `review_ocr_page` | One original page image alongside its saved transcription |
| `record_ocr_review` | Persist an approved, needs-correction, or unreadable verdict |
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

138 tests. Fixtures are compiled from LaTeX so the mathematics has a known
ground truth; regenerate them with `python tests/fixtures/make_fixtures.py`.

The suite covers the promises that matter: a second import runs no engine, a
digital PDF triggers no OCR, a display equation is never split across chunks,
Greek queries match across inflections and across OCR damage, an exact match is
never reported as approximate, images stay inside their token budget, and every
MCP tool answers within its own.

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
- A bundled third-party spelling dictionary for OCR'd Greek prose. The optional
  local-wordlist repair is deliberately constrained to one auditable candidate;
  a broad dictionary will not be bundled without a separate licence review.
- Anything server-shaped: no Postgres, no queue, no web frontend. It is a local
  tool for one person's bookshelf.

---

## Where this is going

`docs/NEXT.md` carries the agreed next steps and the decisions behind them.

## Licence

This project's own source is MIT — see `LICENSE`, which also documents the
copyleft terms of the extraction engines it drives. PyMuPDF is AGPL-3.0 and is a
required dependency; Marker is GPL-3.0 with a commercial-use condition, is
optional, and runs as a subprocess rather than being imported.
