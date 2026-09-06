# Next session

Picking this up cold: read this file, then `README.md` for why the design is
the way it is. Everything below is agreed work, in order.

## Where things stand

- 166 tests pass: `PYTHONPATH=src .venv/bin/python -m pytest tests -q`
- Lint clean: `.venv/bin/ruff check src tests tools benchmarks --select F,E9`
- Both engines work. `pdf-library doctor` should show green for the fast
  engine, Marker and llama-server.
- Everything through `pdf-library repair` is committed and pushed.

The test corpus that matters is a scanned, handwritten Greek maths PDF —
17 pages, integration by parts and partial fractions. Its mathematics extracts
well; its prose does not. That gap is what the work below closes.

## What is still wrong, with examples

These are real, from that document. Keep them as the acceptance cases.

| Class | Example | Plan |
| --- | --- | --- |
| Greek prose OCR confusion | «παραγοντική» → «παραχουτική», «έχουμε» → «έκουμε», «συνάρτηση» → «δυνάρτηση», «γνωστή» → «χνωστή» | **B**, then **A** |
| Lost prime | paper reads `g'(x) = φ(x)`, extraction reads `g(x) = φ(x)` | **C** — flag only |
| Repeated headings | five separate chunks all titled «Βήμα 3» | **D** |
| Underbrace as fraction | `\frac{x \cdot \cos(2x)}{f(x) \cdot g'(x)}` | already flagged; **never auto-fix** |
| Unreachable OCR | «Λύση» → «Νύεν» | not fixable, do not try |

The confusion classes seen in this handwriting, which every heuristic below
should be built around:

```
γ ↔ χ      η ↔ υ ↔ ι      σ ↔ δ ↔ θ      χ ↔ κ      ν ↔ υ
```

---

## C. Flag contradictory definitions

**Why.** `g(x) = ημx → g(x) = −συνx` says one symbol equals two different
things. A machine can see that; it cannot see which side lost the prime.

**Where.** `src/pdf_library/quality.py`, alongside `underbrace_as_fraction`.

**How.** Within a math span, find `X = A` and `X = B` for the same symbol where
`A` and `B` differ, particularly across `\rightarrow` or `\to`. Add issue
`contradictory_definition:<n>` and take a modest score penalty.

**Do not** guess the prime back. Flag, and let the image answer it.

**Verify.** Page 3 of the test document must flag. A page containing an
ordinary chain of equalities (`a = b`, later `b = c`) must not.

Estimated: half an hour.

---

## D. Number repeated headings

**Why.** Five chunks titled «Βήμα 3» are indistinguishable in a search result,
which defeats the point of having headings at all.

**Where.** `src/pdf_library/library.py`, in `reindex_document`, after
`chunk_pages` returns and before the rows are written — the chunker itself
should stay ignorant of its neighbours.

**How.** Group consecutive chunks sharing a heading; if a group has more than
one member, render the heading as `Βήμα 3 (2/5)`. Keep the raw heading in a
separate column if `get_section` needs to match on it — check that
`get_section` still resolves before finishing.

**Verify.** Reindex the test document; no two chunks share a heading string.
`get_section(document, "Βήμα 3")` must still return all five.

Estimated: fifteen minutes.

---

## B. Confusion-constrained Greek spellcheck

The biggest free win. Deterministic, no model.

**The rule that makes it safe.** Correct a word only when **both** hold:

1. it is absent from a Greek lexicon, and
2. **exactly one** lexicon word is reachable from it using only the confusion
   classes above, within two substitutions.

Uniqueness is the whole safety argument. «παραχουτικ» reaches «παραγοντικ»
(χ→γ, υ→ν) and nothing else, so it is safe. A word with two candidates is left
alone and reported instead of guessed.

**Constraints.**

- Never touch anything between `$` delimiters. Reuse the math-span splitting
  already in `greek_math.py`.
- Log every substitution. It must be auditable and reversible.
- Gate it on the document being Greek, as `repair_greek_math` already is.

**Open decision: the lexicon.** Roughly 1–2 MB. Options, in order of
preference:

1. Vendor a Greek word list into the repo. Self-contained, offline, no install.
   Check the licence of whatever list is chosen and record it in `LICENSE`.
2. Optional dependency, downloaded on first use. Keeps the repo small; adds a
   network dependency to a local-first tool.
3. Build a frequency list from the user's own imported documents. No dependency
   at all, but useless on a first import — probably a later refinement rather
   than the starting point.

Decide this first; the rest follows from it.

**Where.** New module, `src/pdf_library/spellfix.py`. Wire it into
`Library.repair_document` and `_store_pages` next to `rejoin_hyphenation` and
`repair_greek_math`, so `pdf-library repair` applies it with no re-OCR.

**Verify.** The words in the table above must be corrected. A page of correct
Greek must come back byte-identical. Every LaTeX span must be untouched — worth
asserting directly.

Estimated: half a day, most of it the lexicon.

---

## A. CorrectionEngine and a local model

Only after B, and only for what B could not resolve uniquely.

**Do not simply enable Marker's `--use_llm`.** It locks us into Marker's prompts
and its service list, and it cannot use a text-only model.

**Instead**, implement the `CorrectionEngine` protocol:

```python
class CorrectionEngine(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def correct_page(
        self,
        markdown: str,
        page_image: bytes | None = None,
        context: str | None = None,
    ) -> str: ...
```

Three reasons this is worth the extra work over Marker's flag:

1. **We control the prompt.** It must say: this is Greek mathematical text
   OCR'd from handwriting; the common confusions are γ↔χ, η↔υ, σ↔δ; correct
   spelling only; never alter anything between `$` delimiters. Marker's generic
   prompt says none of that, and the last constraint matters most.
2. **Text-only models become usable.** The prose errors need Greek knowledge,
   not vision, and a text model is a fraction of the size and cost.
3. **The backend is swappable** — and absent. The system must keep working with
   no model configured at all.

**Two jobs, deliberately separate.**

- *Prose repair* needs no image. This is where nearly all the remaining errors
  are, and it is cheap.
- *Verifying mathematics against the page* needs vision and is much harder.
  Attempt it only after prose repair is working.

**Model recommendation.** Start local and text-only:

- Greek-specific models (Meltemi 7B, Llama-Krikri 8B, both from ILSP / Athena
  Research Center) should beat a much larger general model at this narrow task,
  and run in about 5 GB quantised.
- Fallback: Qwen2.5 14B Instruct.
- Runner: Ollama is simplest; MLX is faster on Apple Silicon if many pages are
  going through it.

For vision later, sized by available memory: Qwen2.5-VL 7B (16 GB), Qwen2.5-VL
32B (32 GB). Hosted alternatives exist and Marker supports them, but note that
free tiers generally train on submitted data — which is the reason local is
first here.

**Non-negotiable.** Never in the default pipeline. Only pages the quality gate
flagged, or a page explicitly named. The system must be fully usable with no
model installed.

---

## Order

1. **C** and **D** — free, deterministic, no new dependencies. Do these first
   so the session starts with something finished.
2. **B** — decide the lexicon, then build.
3. **A** — only what B left unresolved.

## Decisions already made, not to be relitigated

- **Underbrace-as-fraction is flagged, never repaired.** `\frac{g'(x)}{g(x)}`
  is an ordinary logarithmic derivative; any rule aggressive enough to strip
  the artefacts will eventually corrupt correct mathematics. Silently wrong
  maths is worse than a flagged page.
- **No general spellcheck.** Only confusion-constrained, with the uniqueness
  requirement.
- **No LLM in the default pipeline**, and **no image returned automatically**.
  Both are opt-in and priced.
- **Two extraction engines, not three.** A third is weight without a measured
  gain.
