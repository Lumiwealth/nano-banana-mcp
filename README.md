# Image Generator MCP

Provider-neutral MCP boundary for controlled image generation. The repository
retains its historical directory name for compatibility, but the server and
package are now named `image-generator`.

The current containment profile is intentionally strict:

- approved model: `gemini-3.1-flash-image-preview`
- server-controlled output: 1K
- calendar-month creative budget: $100
- caller inputs: prompt, purpose, and aspect ratio only
- prohibited caller inputs: model, quality, resolution, target size, and output format
- Gemini Pro is not present in the runtime allowlist or tool schema
- the exact provider response is saved without cropping, overlays, or repair

Direct Gemini generation can remain unavailable while the account is unpaid;
the local controls and tests do not require a paid call.

## Tools

- `generate_image`: raw image generation
- `edit_image`: regeneration using one or more references
- `usage_report`: current-month usage grouped by purpose

Every generation requires one purpose:
`thumbnail`, `slide`, `email`, `sms`, `website`, or `test`.

## Attribution and budget ledger

The SQLite ledger defaults to
`~/.local/state/image-generator/usage.sqlite3`. It records timestamp, purpose,
caller, a one-way API-key fingerprint, provider, model, resolution, retry count,
estimated/actual cost, elapsed time, and status. It never stores the raw key or
the prompt.

The default monthly ceiling is $100. Rob can manually unlock only in $100
increments by changing `IMAGE_GENERATOR_MONTHLY_BUDGET_USD`. Failed provider
calls conservatively count against the ceiling because providers may bill a
request after accepting it.

## Environment

| Variable | Default |
|---|---|
| `GEMINI_API_KEY` | required for live generation |
| `IMAGE_GENERATOR_OUTPUT_DIR` | `~/Documents/Development/.image_generator_output` |
| `IMAGE_GENERATOR_STATE_DIR` | `~/.local/state/image-generator` |
| `IMAGE_GENERATOR_CALLER` | `creative-image-generator` |
| `IMAGE_GENERATOR_MONTHLY_BUDGET_USD` | `100` |

There is deliberately no environment or tool parameter that enables Gemini Pro
or raises resolution. A future approved provider is selected by changing the
server implementation and its tests after a blind quality/cost benchmark.

## Run and test

```bash
uv sync --dev
uv run pytest
GEMINI_API_KEY=... uv run python server.py
```

Codex configuration should use a provider-neutral key:

```toml
[mcp_servers.image_generator]
command = "uv"
args = ["--directory", "/absolute/path/to/nano-banana-mcp", "run", "python", "server.py"]
env = { GEMINI_API_KEY = "...", IMAGE_GENERATOR_CALLER = "creative-image-generator", IMAGE_GENERATOR_MONTHLY_BUDGET_USD = "100" }
```
