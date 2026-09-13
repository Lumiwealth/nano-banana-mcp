# Image Generator MCP

Provider-neutral MCP boundary for controlled image generation. The server,
package, client registration, and local directory are named `image-generator`.

`Nano Banana` and `nano-banana` are legacy user-language aliases for “use the
approved Image Generator.” They do not select Google, Gemini, or another model.
A provider-specific exception requires an explicit request from Rob.

The current containment profile is intentionally strict:

- approved model: `gpt-image-2.5-sunburst` (the heavier GPT Image 2.5
  variant, better at reference-photo likeness; Flare is the fast one)
- server-controlled resolution: exact 16:9, 1:1, 9:16, 1.91:1 or 4:5 sizes
  (1.91:1 and 4:5 added 2026-09-12 for Google and Meta paid placements)
- default quality: `low`; `medium` and `high` are permitted upgrades
  (Rob authorized `high` on 2026-09-12 for paid advertising: "they are ads,
  we are spending way more than a dollar per image in spend anyway". Measured
  output tokens: 196 low, 439 medium, 1756 high, so high is roughly 5 cents.
  Use `high` for anything that will run as a paid ad or a presentation slide.)
- calendar-month creative budget: $100
- caller inputs: prompt, purpose, aspect ratio, and optional low/medium quality
- prohibited caller inputs: model, resolution, target size, output format, and
  `auto` quality (`auto` is nondeterministic and silently downgrades, which
  would make the ledger and the creative unreproducible)
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

## Model migration history

The server moved through GPT Image 2.5 Flare on September 11, 2026, before
switching to `gpt-image-2.5-sunburst` on September 12 after that model became
available to the project. Flare remains useful history for interpreting old
receipts; Sunburst is the effective server-controlled model.

The project could not call it at first:

```
403 model_not_found
Project `proj_2Sz...` does not have access to model `gpt-image-2.5-flare`
```

The cause was the project's own allowed-models list, which held `gpt-image-2`
and nothing else. It lives in the OpenAI dashboard under the project, then
Limits, then Model usage, then Allowed models. Adding `gpt-image-2.5-flare`
and its dated `gpt-image-2.5-flare-2026-09-08` twin fixed it. The change takes
roughly half a minute to reach the API, so one probe call can still fail right
after saving. This is an OpenAI dashboard setting, not Google Cloud.

**Pin the dated snapshot, not the alias.** `gpt-image-2.5-flare` answers image
generations but returns the same 403 on image edits, which is how most of this
server's real work is done. `gpt-image-2.5-flare-2026-09-08` answers both, so
that is what `APPROVED_MODEL` holds. Test both endpoints after any model change,
not just generation.

If a future model swap returns `model_not_found`, check that list first, then
check that the organization is verified under Organization settings. Never
point `APPROVED_MODEL` at a model the project cannot call: every image tool in
every session fails immediately if you do.
