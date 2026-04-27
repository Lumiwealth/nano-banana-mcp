# Nano Banana MCP Server

Minimal, security-conscious MCP server for Google Gemini image generation.
Defaults to **Nano Banana Pro** (`gemini-3-pro-image-preview`) — Google's
flagship image model. Output is **automatically resized + re-encoded** to
web/email/mobile-optimal defaults so generated images aren't 4K monsters
that blow up your storage and load slowly.

## Tools

- **`generate_image`** — text → image
- **`edit_image`** — text + reference image(s) → image

Both tools accept the same size + format params (see "Output post-processing"
below).

## Environment

| Var | Required | Default |
|---|---|---|
| `GEMINI_API_KEY` | yes | — |
| `NANO_BANANA_DEFAULT_MODEL` | no | `gemini-3-pro-image-preview` |
| `NANO_BANANA_OUTPUT_DIR` | no | `~/Documents/Development/.nano_banana_output` |
| `NANO_BANANA_DEFAULT_SIZE` | no | `1024` |
| `NANO_BANANA_DEFAULT_FORMAT` | no | `webp` |
| `NANO_BANANA_DEFAULT_QUALITY` | no | `75` |

## Allowed models

Hardcoded allowlist — old/cheap models cannot be selected even if requested:

- `gemini-3-pro-image-preview` (Nano Banana Pro, flagship)
- `gemini-3.1-flash-image-preview` (Nano Banana 2, newest Flash, 4K)

## Output post-processing

Every generated image is downscaled + re-encoded by default. Override per
call via `target_size`, `output_format`, `quality`.

### Why post-process

Gemini returns 1024-2048px PNGs (or 4K for the Flash model). For email
+ mobile + web use cases, that's 10-20× more bytes than needed. Post-
processing in the MCP keeps the rest of the pipeline simple — callers
get a tiny WebP they can drop straight into a `<img src>` and ship.

A 4K source PNG (~28 KB for solid color, **1-3 MB for photographic content**)
becomes a 1024 WebP at quality 75 in the **30-50 KB range** for hero email
images. ~95% size reduction, visually indistinguishable in any email client.

### `target_size`

Max output dimensions. Aspect ratio is preserved; image is **only**
downscaled, never upscaled (we never add detail that isn't in the source).

| Value | Meaning |
|---|---|
| `256` | 256×256 max — thumbnails, avatars |
| `512` | 512×512 max — small product cards |
| `640` | 640×640 max — MMS-friendly |
| `1024` *(default)* | 1024×1024 max — email hero, social, mobile-first web |
| `2048` / `2k` | 2048×2048 max — desktop hero, print preview |
| `4k` | 3840×3840 max — print, billboard, "show me everything" |
| `WIDTHxHEIGHT` | exact custom max (e.g. `600x300` for a 2:1 hero strip) |
| `off` | passthrough — keep raw model PNG, no resize, no re-encode |

### `output_format`

| Value | Use when |
|---|---|
| `webp` *(default)* | Email, web, mobile. 25-35% smaller than JPEG at the same visual quality. Supported in every modern email client (Gmail / Apple / Outlook web / iOS / Android Mail) since 2023, and 100% of modern browsers. |
| `jpeg` | Old Outlook (≤2019) compatibility, MMS payloads (carrier WebP support varies), or anywhere `<picture>` fallback isn't possible. |
| `png` | Lossless or transparency required. Much larger files — only use when alpha matters. |

### `quality`

Integer 30-100 (WebP/JPEG only; ignored for PNG).

- **75** — default. Sweet spot for email + web.
- **85-90** — premium landing-page hero, when file size is less critical.
- **60-65** — extreme bandwidth constraints; expect mild banding on gradients.

### Example calls

```python
# Email hero, default 1024×1024 max → WebP @ 75 (smallest acceptable)
generate_image(prompt="a stylized AI trading bot with a clipboard")

# Wide email banner — exact aspect
generate_image(
    prompt="paywall checkout almost-there illustration",
    target_size="600x300",
)

# Old Outlook compatibility for a legacy customer cohort
generate_image(prompt="...", output_format="jpeg", quality=80)

# Print-quality marketing collateral
generate_image(prompt="...", target_size="4k", output_format="png")

# Skip resize entirely (keep the raw 4K Gemini PNG)
generate_image(prompt="...", target_size="off")
```

## Security model

- API key only loaded from env, never logged.
- Output is locked to a single sandboxed directory; filenames are timestamped
  and validated against `[A-Za-z0-9._-]`.
- Path traversal is rejected; final write path must live directly in
  `NANO_BANANA_OUTPUT_DIR`.
- Reference images: validated MIME by extension, max 20 MB each.
- Model allowlist prevents downgrade to older/cheaper models.
- No shell execution anywhere in the code path.

## Run locally

```bash
cd mcp_servers/nano_banana
uv sync
GEMINI_API_KEY=... uv run python server.py
```

## Wire into Claude Code

Registered via `claude mcp add` — see project setup.

## Why this exists in MCP form

Calling Gemini's image API directly from inside another agent's session
means re-implementing safety (model allowlist, output-dir sandboxing,
filename validation, prompt logging) every time. The MCP wraps all of
that once and exposes it as two tools. Side benefit: any agent that
already speaks MCP gets image generation for free.
