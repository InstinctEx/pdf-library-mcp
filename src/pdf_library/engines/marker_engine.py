"""Quality extraction path, built on Marker.

Marker is run out-of-process through its ``marker_single`` CLI rather than
imported. Two reasons: it drags in torch and a model set that we do not want
resident inside a long-lived MCP server, and keeping it behind a process
boundary means a crash or an OOM in the model stack cannot take the server
down with it.

This engine is optional. Nothing in the core pipeline requires it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..blocks import page_methods_from_meta, parse_document
from ..mathfix import repair_html_scripts
from .base import (
    EngineUnavailable,
    ExtractedPage,
    ExtractionResult,
)

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
        if not zero_based:
            raise EngineUnavailable("at least one positive page number is required")
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
        """Run Marker and return per-page Markdown, blocks and OCR provenance.

        JSON is requested rather than Markdown: it carries block bounding
        boxes, block types, and how each page's text was obtained, none of
        which survive Marker's Markdown renderer.
        """
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
                "json",
                # Nothing here consumes extracted images, and skipping them
                # saves both time and a great deal of temporary disk.
                "--disable_image_extraction",
                "--disable_tqdm",
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

            document, meta = self._read_output(out_dir)

        page_blocks = parse_document(document, page_methods_from_meta(meta))
        extracted: list[ExtractedPage] = []
        for page in page_blocks:
            # Marker emits real LaTeX for equations but still leaves stray
            # HTML scripts in prose, so the same repair applies here.
            markdown = repair_html_scripts(page.markdown)
            extracted.append(
                ExtractedPage(
                    page_number=page.page_number,
                    markdown=markdown,
                    ocr_used=page.ocr_used,
                    ocr_reason="surya" if page.ocr_used else None,
                    blocks=page.blocks,
                    page_width=page.width,
                    page_height=page.height,
                )
            )

        if pages and len(extracted) == len(pages):
            # Marker numbers pages from its own page_range; trust the caller's
            # numbering when the counts line up exactly.
            for target, page in zip(sorted(pages), extracted):
                page.page_number = target
                for block in page.blocks:
                    block.page_number = target

        extracted.sort(key=lambda p: p.page_number)
        return ExtractionResult(
            engine=self.name,
            engine_version=self.version(),
            pages=[p for p in extracted if p.markdown],
            duration_s=time.perf_counter() - started,
        )

    @staticmethod
    def _read_output(out_dir: Path) -> tuple[dict, dict]:
        candidates = [p for p in out_dir.rglob("*.json") if "_meta" not in p.name]
        if not candidates:
            raise EngineUnavailable("marker_single produced no JSON output")
        document = json.loads(candidates[0].read_text(encoding="utf-8"))
        meta_path = candidates[0].with_name(candidates[0].stem + "_meta.json")
        meta = {}
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return document, meta
