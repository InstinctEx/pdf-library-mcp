"""Local vision transcription through an MLX-VLM OpenAI-compatible server."""

from __future__ import annotations

import base64
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import pymupdf

from .config import VisionConfig
from .engines.base import EngineUnavailable


@dataclass
class VisionCorrection:
    page: int
    markdown: str
    confidence: float
    warnings: list[str]
    model: str
    validation_warnings: list[str]


class VisionEngine:
    """Ask a local VLM to transcribe the source image, not solve it."""

    def __init__(self, config: VisionConfig) -> None:
        self.config = config

    def available(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "vision correction disabled"
        try:
            req = urllib.request.Request(self.config.base_url.rstrip("/") + "/models")
            with urllib.request.urlopen(req, timeout=3) as response:
                if response.status == 200:
                    return True, self.config.model
        except Exception as exc:
            return False, f"MLX-VLM unavailable at {self.config.base_url}: {exc}"
        return False, f"MLX-VLM unavailable at {self.config.base_url}"

    def correct(
        self, pdf_path: Path, page_number: int, transcript: str, enhanced: bool = False
    ) -> VisionCorrection:
        images = self._render(pdf_path, page_number, enhanced=enhanced)
        prompt = (
            "Transcribe this handwritten mathematics page exactly from the image. "
            "The image is authoritative; the existing OCR transcript below is "
            "untrusted and must not be copied when it conflicts with the image. "
            "Do not solve, "
            "explain, normalize, or invent anything. Preserve every Greek word, "
            "symbol, exponent, minus sign, fraction, and function name. Return "
            "clean Markdown with LaTeX for mathematics. If genuinely unreadable, "
            "write [unreadable] rather than guessing. Return JSON with keys "
            "markdown, confidence (0 to 1), and warnings (array of strings).\n\n"
            "Existing OCR transcript (comparison aid only):\n" + transcript
        )
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *[
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}
                        for image in images
                    ],
                ],
            }],
        }
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise EngineUnavailable(f"MLX-VLM request failed: {exc}") from exc
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EngineUnavailable("MLX-VLM returned no message content") from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        parsed = self._parse(str(content))
        correction = VisionCorrection(
            page=page_number,
            markdown=parsed["markdown"].strip(),
            confidence=max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
            warnings=[str(w) for w in parsed.get("warnings", [])],
            model=self.config.model,
            validation_warnings=[],
        )
        return correction

    def _render(self, pdf_path: Path, page_number: int, enhanced: bool = False) -> list[str]:
        with pymupdf.open(pdf_path) as document:
            if not 1 <= page_number <= document.page_count:
                raise EngineUnavailable(f"page {page_number} is outside the PDF")
            page = document[page_number - 1]
            scale = self.config.render_scale
            rect = page.rect
            clips = [None]
            if enhanced and self.config.crop_retry:
                overlap = rect.height * 0.08
                split = rect.height / 2
                clips += [
                    pymupdf.Rect(rect.x0, rect.y0, rect.x1, min(rect.y1, split + overlap)),
                    pymupdf.Rect(rect.x0, max(rect.y0, split - overlap), rect.x1, rect.y1),
                ]
            images = []
            for clip in clips:
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False
                )
                images.append(base64.b64encode(pixmap.tobytes("jpeg", jpg_quality=90)).decode())
            return images

    @staticmethod
    def validate(
        original: str,
        candidate: VisionCorrection,
        greek_document: bool = False,
        min_greek_ratio: float = 0.35,
    ) -> list[str]:
        """Return deterministic reasons why a candidate must not be applied."""
        warnings = list(candidate.warnings)
        text = candidate.markdown.strip()
        if not text:
            warnings.append("empty_model_output")
        if candidate.confidence <= 0:
            warnings.append("missing_or_invalid_confidence")
        if "[unreadable]" in text.lower():
            warnings.append("contains_unreadable_placeholder")
        if re.search(r"\b(?:h|eivar|autigropu|tus|unerbod)\b", text, re.IGNORECASE):
            warnings.append("latin_transliteration_detected")
        if text.count("{") != text.count("}"):
            warnings.append("unbalanced_latex_braces")
        if text.count("$$") % 2 or text.count("$") % 2:
            warnings.append("unbalanced_math_delimiters")
        original_math = _math_inventory(original)
        candidate_math = _math_inventory(text)
        if original_math and candidate_math < max(1, int(original_math * 0.70)):
            warnings.append(f"formula_inventory_dropped:{original_math}->{candidate_math}")
        if len(original.strip()) >= 80 and len(text) < int(len(original.strip()) * 0.45):
            warnings.append("candidate_too_short")
        if greek_document:
            prose = re.sub(r"\$\$.*?\$\$|(?<!\\)\$.*?(?<!\\)\$", " ", text, flags=re.S)
            letters = [ch for ch in prose if ch.isalpha()]
            greek = [ch for ch in letters if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿"]
            if letters and len(greek) / len(letters) < min_greek_ratio:
                warnings.append("greek_prose_ratio_too_low")
        return sorted(set(warnings))

    @staticmethod
    def _parse(content: str) -> dict:
        cleaned = content.strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                value = json.loads(match.group(0))
                if isinstance(value, dict) and value.get("markdown"):
                    return value
            except json.JSONDecodeError:
                pass
        # Models occasionally ignore the JSON request. Preserve the transcription
        # rather than dropping it, but make it ineligible for automatic apply.
        return {
            "markdown": re.sub(r"^```(?:markdown)?|```$", "", cleaned).strip(),
            "confidence": 0.0,
            "warnings": ["non-JSON model response"],
        }


def _math_inventory(text: str) -> int:
    return len(re.findall(r"\\(?:frac|sqrt|int|ln|sin|cos|sinh|cosh|tanh|coth|sech|csch)|\$", text))
