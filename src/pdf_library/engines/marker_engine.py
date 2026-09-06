"""Quality extraction path, built on Marker.

Marker is run out-of-process through its ``marker_single`` CLI rather than
imported. Two reasons: it drags in torch and a model set that we do not want
resident inside a long-lived MCP server, and keeping it behind a process
boundary means a crash or an OOM in the model stack cannot take the server
down with it.

This engine is optional. Nothing in the core pipeline requires it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..mathfix import repair_html_scripts
from .base import (
    EngineUnavailable,
    ExtractedPage,
    ExtractionResult,
)

# marker's --paginate_output inserts a marker line between pages.
_PAGE_SEPARATOR = re.compile(r"^\{(\d+)\}-{20,}\s*$", re.MULTILINE)

# Failures worth translating into an instruction the user can act on.
_KNOWN_FAILURES: tuple[tuple[str, str], ...] = (
    (
        "llama-server binary not found",
        "marker's model runner (surya) needs the llama.cpp server binary. "
        "Install it with 'brew install llama.cpp', or point $LLAMA_CPP_BINARY "
        "at an existing llama-server.",
    ),
    (
        "CUDA out of memory",
        "the GPU ran out of memory. Reprocess fewer pages at a time, or set "
        "torch_device = \"cpu\" in the [marker] config section.",
    ),
    (
        "MPS backend out of memory",
        "the GPU ran out of memory. Reprocess fewer pages at a time, or set "
        "torch_device = \"cpu\" in the [marker] config section.",
    ),
)


def _explain(output: str | None) -> str:
    """Turn marker's traceback into one actionable line.

    The last line of a Python traceback is often the least useful part of it --
    a hint from an exception message rather than the failure itself -- so look
    for a known cause first and fall back to the exception line.
    """
    text = (output or "").strip()
    if not text:
        return "no output"
    for needle, explanation in _KNOWN_FAILURES:
        if needle in text:
            return explanation
    for line in reversed(text.splitlines()):
        # The exception line of a traceback: "module.Error: message".
        if re.match(r"^\w[\w.]*(Error|Exception)\b", line.strip()):
            return line.strip()
    return text.splitlines()[-1].strip()


class MarkerEngine:
    name = "marker"
    quality_tier = "high"

    def __init__(
        self,
        executable: str = "marker_single",
        torch_device: str = "",
        timeout_seconds: int = 3600,
        extra_args: list[str] | None = None,
    ) -> None:
        self.executable = executable
        self.torch_device = torch_device
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])

    # ------------------------------------------------------------------
    def _resolve(self) -> str | None:
        """Find the marker binary.

        PATH alone is not enough: an MCP server started by its absolute path
        from a virtualenv usually does not have that virtualenv's bin directory
        on PATH, even though marker was installed into it. So look beside the
        running interpreter first.
        """
        candidate = Path(self.executable)
        if candidate.is_absolute():
            return str(candidate) if candidate.is_file() else None

        beside = Path(sys.executable).parent / self.executable
        if beside.is_file():
            return str(beside)
        return shutil.which(self.executable)

    def available(self) -> tuple[bool, str]:
        path = self._resolve()
        if not path:
            return False, (
                f"{self.executable} not on PATH "
                "(install with: pip install marker-pdf)"
            )
        return True, path

    def version(self) -> str:
        try:
            import importlib.metadata as md

            return md.version("marker-pdf")
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.torch_device:
            env["TORCH_DEVICE"] = self.torch_device
        # Marker's tokenizers emit a fork warning that pollutes MCP stderr.
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        return env

    def _page_range(self, pages: list[int]) -> str:
        """Marker's --page_range is 0-based and accepts ranges like 0-4,7."""
        zero_based = sorted({p - 1 for p in pages if p >= 1})
        parts: list[str] = []
        start = prev = zero_based[0]
        for value in zero_based[1:]:
            if value == prev + 1:
                prev = value
                continue
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = value
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        return ",".join(parts)

    def extract(
        self, pdf_path: Path, pages: list[int] | None = None
    ) -> ExtractionResult:
        binary = self._resolve()
        if not binary:
            raise EngineUnavailable(self.available()[1])

        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="pdflib-marker-") as tmp:
            out_dir = Path(tmp)
            cmd = [
                binary,
                str(pdf_path),
                "--output_dir",
                str(out_dir),
                "--output_format",
                "markdown",
                "--paginate_output",
                *self.extra_args,
            ]
            if pages:
                cmd += ["--page_range", self._page_range(pages)]

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=self.timeout_seconds,
            )
            if proc.returncode != 0:
                raise EngineUnavailable(
                    "marker_single failed: " + _explain(proc.stderr or proc.stdout)
                )

            md_files = sorted(out_dir.rglob("*.md"))
            if not md_files:
                raise EngineUnavailable("marker_single produced no markdown")
            raw = md_files[0].read_text(encoding="utf-8")

        extracted = self._split_pages(raw, requested=pages)
        return ExtractionResult(
            engine=self.name,
            engine_version=self.version(),
            pages=extracted,
            duration_s=time.perf_counter() - started,
        )

    # ------------------------------------------------------------------
    def _split_pages(
        self, markdown: str, requested: list[int] | None
    ) -> list[ExtractedPage]:
        """Turn marker's paginated output back into per-page Markdown.

        The separator carries marker's own 0-based page index, which stays
        correct under --page_range, so we trust it and only fall back to
        positional mapping when the separators are missing.
        """
        matches = list(_PAGE_SEPARATOR.finditer(markdown))
        if not matches:
            targets = requested or [1]
            return [ExtractedPage(page_number=targets[0], markdown=markdown.strip())]

        pages: list[ExtractedPage] = []
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            # Marker emits real LaTeX for equations but still leaves stray
            # HTML scripts in prose, so the same repair applies here.
            body = repair_html_scripts(markdown[start:end].strip())
            number = int(match.group(1)) + 1
            pages.append(ExtractedPage(page_number=number, markdown=body))

        # Leading content before the first separator belongs to that page.
        head = markdown[: matches[0].start()].strip()
        if head and pages:
            pages[0] = ExtractedPage(
                page_number=pages[0].page_number,
                markdown=f"{head}\n\n{pages[0].markdown}".strip(),
            )

        if requested and len(pages) == len(requested):
            # Trust the caller's numbering when the counts line up exactly.
            ordered = sorted(requested)
            pages = [
                ExtractedPage(page_number=ordered[i], markdown=p.markdown)
                for i, p in enumerate(pages)
            ]

        return [p for p in pages if p.markdown]
