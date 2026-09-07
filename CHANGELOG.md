
## 2026-09-07

### Added
- MLX-VLM vision OCR engine for local Apple Silicon inference.
- High-resolution page rendering for OCR verification and correction.
- OCR correction and verification flow using page images plus existing OCR text.
- Support for Greek handwritten mathematics and LaTeX-preserving transcription prompts.
- Confidence-based correction application with warnings and review status.
- Provenance storage for original OCR, corrected OCR, model, confidence, and timestamps.
- MCP tools for correcting a single OCR page and multiple pages.
- MLX-VLM configuration options in the PDF Library config.
- Documentation for MLX-VLM setup and usage.

### Changed
- OCR pipeline can now use a local vision-language model as a correction layer after existing extraction.
- Corrected pages are reindexed after accepted OCR corrections.
- Existing OCR is preserved instead of being destructively replaced.

### Notes
- Designed for local MLX-VLM usage on Apple Silicon.
- Recommended starting model: `mlx-community/Qwen3-VL-8B-Instruct-4bit`.
