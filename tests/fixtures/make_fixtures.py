"""Build the test PDFs.

Two of them are compiled from LaTeX so the tests have a known ground truth for
the mathematics; one is a synthetic scan with no text layer at all. Regenerate
with:

    python tests/fixtures/make_fixtures.py

The generated PDFs are committed, so pdflatex is only needed when the sources
change.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent

MATH_TEX = r"""
\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath, amssymb, amsthm}
\newtheorem{theorem}{Theorem}
\begin{document}
\section{Convergence}
Let $f \in L^1(\mathbb{R})$ and suppose $f_n \to f$ pointwise.

\begin{theorem}[Dominated Convergence]
If $|f_n| \le g$ with $g \in L^1$, then
\begin{equation}
\lim_{n \to \infty} \int_{-\infty}^{\infty} f_n(x)\,dx
  = \int_{-\infty}^{\infty} f(x)\,dx .
\end{equation}
\end{theorem}

\begin{proof}
Apply Fatou's lemma to $g + f_n$ and to $g - f_n$:
\begin{align}
\int g + \int f &\le \liminf \int (g + f_n) \\
                &= \int g + \liminf \int f_n .
\end{align}
\end{proof}

\newpage
\section{Assorted formulas}
A piecewise definition,
\[
f(x) = \begin{cases} x^2 & x \ge 0, \\ -x^2 & x < 0, \end{cases}
\]
a matrix,
\[
A = \begin{pmatrix} a_{11} & a_{12} \\ a_{21} & a_{22} \end{pmatrix},
\]
and a sum with Greek letters: $\sum_{k=1}^{n} \alpha_k \beta_k \le \gamma$.
The complexity is $O(n^2)$ and the radius is $r^{4/3}$.
\end{document}
"""

GREEK_TEX = r"""
\documentclass[11pt]{article}
\usepackage{fontspec}
\usepackage{amsmath, amssymb}
\setmainfont{Times New Roman}
\begin{document}
\section{Ακολουθίες και σειρές}
Έστω η ακολουθία $a_n$ με $a_n \to L$. Η συνάρτηση είναι συνεχής παντού.

\subsection{Ορισμός}
Λέμε ότι η σειρά συγκλίνει όταν
\[
\sum_{n=1}^{\infty} a_n = \lim_{N \to \infty} \sum_{n=1}^{N} a_n
\]
υπάρχει και είναι πεπερασμένο. Η ακτίνα σύγκλισης της δυναμοσειράς είναι $R$.
\end{document}
"""


def _compile(tex: str, name: str, engine: str) -> Path | None:
    binary = shutil.which(engine)
    if not binary:
        print(f"skip {name}: {engine} not installed", file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "doc.tex").write_text(tex, encoding="utf-8")
        proc = subprocess.run(
            [binary, "-interaction=nonstopmode", "-halt-on-error", "doc.tex"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        produced = work / "doc.pdf"
        if not produced.is_file():
            tail = (proc.stdout or "").strip().splitlines()[-8:]
            print(f"skip {name}: {engine} failed\n" + "\n".join(tail), file=sys.stderr)
            return None
        target = HERE / name
        shutil.copy2(produced, target)
        print(f"wrote {target}")
        return target


def make_scanned() -> Path:
    """A PDF whose pages are images only, so it has no text layer."""
    import pymupdf

    doc = pymupdf.open()
    for text in ("Scanned page one", "Scanned page two"):
        # Render the text to a pixmap, then place only the image on the page,
        # which is exactly what a scanner produces.
        temp = pymupdf.open()
        temp_page = temp.new_page(width=595, height=842)
        temp_page.insert_text((72, 200), text, fontsize=28)
        pixmap = temp_page.get_pixmap(dpi=72)
        temp.close()

        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, pixmap=pixmap)

    target = HERE / "scanned.pdf"
    doc.save(target, deflate=True, garbage=4)
    doc.close()
    print(f"wrote {target}")
    return target


if __name__ == "__main__":
    _compile(MATH_TEX, "math.pdf", "pdflatex")
    _compile(GREEK_TEX, "greek.pdf", "xelatex")
    make_scanned()
