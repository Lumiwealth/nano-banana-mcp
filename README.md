# Image Generator MCP

Provider-neutral MCP boundary for controlled image generation. The repository
retains its historical directory name for compatibility, but the server and
package are now named `image-generator`.

The current containment profile is intentionally strict:

- approved model: `gpt-image-2`
- server-controlled resolution: exact 16:9, 1:1, or 9:16 sizes
- default quality: `low`; the only permitted upgrade is `medium`
- calendar-month creative budget: $100
- caller inputs: prompt, purpose, aspect ratio, and optional low/medium quality
- prohibited caller inputs: model, resolution, target size, output format, high
  quality, and auto quality
- the exact provider response is saved without cropping, overlays, or repair

## Tools

- `generate_image`: raw image generation
- `edit_image`: regeneration using one or more references
- `usage_report`: current-month usage grouped by purpose

Every generation requires one purpose:
`thumbnail`, `slide`, `email`, `sms`, `website`, or `test`.

## Attribution and budget ledger

The SQLite ledger defaults to
`~/.local/state/image-generator/usage.sqlite3`. It records timestamp, purpose,
caller, a one-way API-key fingerprint, provider, model, quality, resolution,
retry count, estimated/actual cost, elapsed time, and status. It never stores
the raw key or the prompt.

The default monthly ceiling is $100. Rob can manually unlock only in $100
increments by changing `IMAGE_GENERATOR_MONTHLY_BUDGET_USD`. Failed provider
calls conservatively count against the ceiling because providers may bill a
request after accepting it.

## Environment

| Variable | Default |
|---|---|
| `OPENAI_API_KEY` | required for live generation |
| `IMAGE_GENERATOR_OUTPUT_DIR` | `~/Documents/Development/.image_generator_output` |
| `IMAGE_GENERATOR_STATE_DIR` | `~/.local/state/image-generator` |
| `IMAGE_GENERATOR_CALLER` | `creative-image-generator` |
| `IMAGE_GENERATOR_MONTHLY_BUDGET_USD` | `100` |

There is deliberately no environment or tool parameter that changes the model
or resolution. `low` is used when quality is omitted. `medium` may be selected
for a final asset when Rob asks for it or an inspected low-quality result is
insufficient; `high` and `auto` are unavailable.

## Run and test

```bash
uv sync --dev
uv run pytest
OPENAI_API_KEY=... uv run python server.py
```

Codex and Claude should launch the checked-in wrapper, which loads the approved
local credential source without copying a raw key into either MCP config:

```bash
IMAGE_GENERATOR_CALLER=creative-image-generator ./run.sh
```
