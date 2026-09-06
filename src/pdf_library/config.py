"""Configuration loading.

Config is read from (in order of precedence):
  1. the path in $PDF_LIBRARY_CONFIG
  2. <library_root>/config.toml
  3. built-in defaults

The library root itself may be overridden with $PDF_LIBRARY_ROOT.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("~/Documents/pdf-library").expanduser()


@dataclass(frozen=True)
class ExtractionConfig:
    # Engine used on import. Always fast so the document is searchable at once.
    fast_engine: str = "pymupdf4llm"
    # Engine used for the background quality upgrade. None disables upgrades.
    quality_engine: str | None = "marker"
    # Upgrade pages whose fast-path quality score is below this.
    upgrade_below_score: float = 0.75
    # Also upgrade any page holding math, regardless of score.
    upgrade_math_pages: bool = True
    # Start the upgrade automatically after a successful import.
    auto_upgrade: bool = False
    # Cap on pages sent to the quality engine in one upgrade job (0 = no cap).
    max_upgrade_pages: int = 0


@dataclass(frozen=True)
class MarkerConfig:
    # Marker runs out-of-process: it pulls in torch and its own model set.
    executable: str = "marker_single"
    # "" lets marker choose; "mps", "cuda" or "cpu" force a device.
    torch_device: str = ""
    timeout_seconds: int = 3600
    extra_args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchConfig:
    max_results: int = 10
    snippet_chars: int = 320


@dataclass(frozen=True)
class ResponseConfig:
    """Ceilings that keep MCP replies from flooding the model's context."""

    max_search_results: int = 10
    max_snippet_chars: int = 320
    max_page_chars: int = 12000
    max_response_tokens: int = 6000
    # Ceiling for an explicitly requested page image. Images are far more
    # expensive than text and persist in the conversation, so this is a hard
    # cap rather than a default.
    max_image_tokens: int = 900


@dataclass(frozen=True)
class ChunkingConfig:
    target_chars: int = 1800
    max_chars: int = 4000


@dataclass(frozen=True)
class Config:
    root: Path = DEFAULT_ROOT
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    marker: MarkerConfig = field(default_factory=MarkerConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    response: ResponseConfig = field(default_factory=ResponseConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)

    @property
    def documents_dir(self) -> Path:
        return self.root / "documents"

    @property
    def db_path(self) -> Path:
        return self.root / "library.db"

    def ensure_dirs(self) -> None:
        self.documents_dir.mkdir(parents=True, exist_ok=True)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _apply(dc: Any, values: dict[str, Any]) -> Any:
    """Replace only the fields the config file actually names."""
    known = {f for f in dc.__dataclass_fields__}
    return replace(dc, **{k: v for k, v in values.items() if k in known})


def load_config(path: Path | None = None) -> Config:
    root_override = os.environ.get("PDF_LIBRARY_ROOT")
    root = Path(root_override).expanduser() if root_override else DEFAULT_ROOT

    if path is None:
        env_path = os.environ.get("PDF_LIBRARY_CONFIG")
        path = Path(env_path).expanduser() if env_path else root / "config.toml"

    data: dict[str, Any] = {}
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    library = _section(data, "library")
    if not root_override and "root" in library:
        root = Path(str(library["root"])).expanduser()

    return Config(
        root=root,
        extraction=_apply(ExtractionConfig(), _section(data, "extraction")),
        marker=_apply(MarkerConfig(), _section(data, "marker")),
        search=_apply(SearchConfig(), _section(data, "search")),
        response=_apply(ResponseConfig(), _section(data, "response")),
        chunking=_apply(ChunkingConfig(), _section(data, "chunking")),
    )
