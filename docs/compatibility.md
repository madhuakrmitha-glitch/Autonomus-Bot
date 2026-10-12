# Compatibility record

The spec asks to install maintained upstream components at tested versions and record version, model
revision, license, adapter capabilities and passing probes (§6), upgrading one integration at a time.
Python dependencies are locked in `uv.lock`; the versions below are those the fixture suite passed with.

## Components

| Component | Version | Role | How verified |
|---|---|---|---|
| Python | 3.11+ (tested 3.13) | Coordinator and worker runtime | Fixture suite |
| OpenClaw | 2026.10.2 (pinned) | Gateway, Telegram, conversation | Plugin type-checked against its SDK `.d.ts`; runtime needs Node ≥ 24.16 |
| Playwright (Python) | 1.56.0 (Chromium build 1194) | Browser observation and execution (S07, S08) | AT04-AT06, AT10, AT11, AT13, AT22 |
| FastAPI / Uvicorn | 0.143 / 0.54 | Controller and worker API | API tests, `omca run` smoke |
| MCP Python SDK | 2.3 (`mcp>=2.3,<3`) | stdio MCP server for OpenClaw | Tool registration test |
| python-docx | 1.2.0 | Word report helper (S22) | AT09 |
| openpyxl | 3.1.5 | Spreadsheet adapter | AT24 |
| yt-dlp | 2026.8.19 | Accessible media download (S14) | AT16 (HTML5 page with caption track) |
| FFmpeg / ffprobe | 6.1 in CI; any recent build on Windows | Frames, audio, scene detection (S15) | AT16-AT20 |
| faster-whisper | 1.2.1 | Speech recognition (S16) | Code path only; weights must be pre-downloaded |
| httpx | 0.28.1 | Provider, search and Telegram clients | Mock-transport tests |

## Model routes (fill in at kickoff)

A route serves application roles only after `omca models probe <route>` passes for its current
configuration digest (stored in `route_probes`). Record the result here when a route is enabled.

| Route id | Provider | Model / revision | License (checked on model card) | Modalities | Probes passed (date) | Notes |
|---|---|---|---|---|---|---|
| local-qwen35-9b | Ollama | qwen3.5:9b / *tag digest* | *verify* | text, image | *pending* | Benchmark context length and quantisation on the chosen hardware |
| deepinfra-qwen35-35b | DeepInfra (OpenAI-compatible) | Qwen/Qwen3.5-35B-A3B | *verify* | text, image | *pending* | Needs prices + source/date and USD→INR FX before it can be enabled |
| hf-router-qwen35-35b | HF Inference Providers | Qwen/Qwen3.5-35B-A3B | *verify* | text, image | *pending* | Confirm image + tool support for the selected provider |
| hf-dedicated-qwen35 | HF dedicated endpoint (TGI/vLLM) | *engine/model* | *verify* | text, image | *pending* | Time-billed: reserve windows with `omca endpoint start` |

Probe set (all required): `text`, `screenshot_interpretation`, `image_with_tools`, `structured_tool_call`,
`tool_result_continuation`, `bounded_output`, `usage_reporting`, `disabled_route_rejection`. Native video
input is an optional capability and is not used by the portable pipeline.

Also record each vision route's `coordinate_system` (how it reports click points: `normalized_1000`,
`pixels` or `normalized_1`) after the live grounding scorecard (`tests/test_live.py`).

## Adapter capabilities

| Adapter | Operations | Notes |
|---|---|---|
| Firecrawl (v2) | search, retrieve (`markdown`, `html`, `rawHtml`, `links`, `screenshot`) | Supplier AI formats (`json`, `summary`, extract) are never requested |
| SearXNG | search | Self-hosted, free |
| HTTP retrieval | retrieve | stdlib HTML extraction, no cookies |
| Browser (worker) | observe, read, navigate, click, click_point, hit_test, type, press, select, scroll, wait, back, fetch_image, download, video_frames, release | Authorized attachments only |
| Media (worker) | media.fetch | Refuses DRM/login-gated media, playlists and live streams |
| Office (worker) | spreadsheet.read/write (copy), email.draft/send, calendar.draft/invite, desktop.raw_input (Windows, approval) | SMTP account per worker |
