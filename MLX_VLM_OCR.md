# MLX-VLM OCR correction

This build adds an optional local vision verification/correction stage for scanned and handwritten PDF pages.

## What changed

- New `VisionConfig` section with MLX-VLM endpoint/model/render/confidence settings.
- New `MLXVLMEngine` using the local OpenAI-compatible `/v1/chat/completions` API.
- Strict Greek + mathematical transcription prompt with JSON-schema output.
- New lossless high-resolution `render_for_ocr()` path; it is separate from MCP's token-budgeted page renderer.
- New `vision_corrections` SQLite audit table preserving the prior and proposed Markdown, model, confidence, warnings, status, timestamp and whether a correction was applied.
- New `Library.correct_ocr_page()`, `correct_ocr_pages()` and `vision_correction_history()` APIs.
- New MCP tools `correct_ocr_page` and `correct_ocr_pages`.
- Confident corrections update page Markdown, document Markdown, quality metadata and the search index.
- Low-confidence/uncertain results never overwrite canonical page text and remain in the OCR review queue.
- New vision tests and updated MCP tool registration test.

## Mac setup

```bash
python3 -m venv ~/.venvs/pdf-vision
source ~/.venvs/pdf-vision/bin/activate
pip install -U mlx-vlm

python -m mlx_vlm.server \
  --model mlx-community/Qwen3-VL-8B-Instruct-4bit
```

Then add to your PDF Library `config.toml`:

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

Restart `pdf-library-mcp` after changing the configuration.

## MCP usage

Dry-run one page first:

```text
correct_ocr_page(document="ΜΑΘΗΜΑΤΙΚΑ Ι ΜΑΘΗΜΑ 10.pdf", page=1, apply=false)
```

Then apply a small batch:

```text
correct_ocr_pages(document="ΜΑΘΗΜΑΤΙΚΑ Ι ΜΑΘΗΜΑ 10.pdf", pages=[1,2,3,4], apply=true)
```

Batch calls are intentionally capped at 20 pages.
