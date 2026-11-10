#!/usr/bin/env bash
set -euo pipefail

repo_dir="/Users/robertgrzesik/Development/mcp_servers/image_generator"
credentials_file="/Users/robertgrzesik/Development/botspot_agent/.env"

if [[ ! -f "$credentials_file" ]]; then
  echo "Missing approved credential source: $credentials_file" >&2
  exit 1
fi

set -a
source "$credentials_file"
set +a

# Image generation is approved production creative work (Rob, 2026-11-10). The
# BotSpot Agent development key above was intentionally disabled by the
# 2026-11-06 zero-paid-AI-in-development rule, so the generator uses its own
# key. Preferred: a dedicated image-only key in ~/.config/image-generator/
# openai-api-key (one line, mode 600). Until that exists, fall back to the
# Media Studio pipeline key, which is a production creative key.
dedicated_key_file="${HOME}/.config/image-generator/openai-api-key"
media_studio_env="/Users/robertgrzesik/Development/botspot-media-studio/.env"
if [[ -s "$dedicated_key_file" ]]; then
  IMAGE_GENERATOR_OPENAI_API_KEY="$(tr -d '[:space:]' < "$dedicated_key_file")"
  export IMAGE_GENERATOR_OPENAI_API_KEY
elif [[ -f "$media_studio_env" ]]; then
  IMAGE_GENERATOR_OPENAI_API_KEY="$(sed -n 's/^[[:space:]]*OPENAI_API_KEY[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' "$media_studio_env" | tail -1)"
  export IMAGE_GENERATOR_OPENAI_API_KEY
fi

export IMAGE_GENERATOR_OUTPUT_DIR="${IMAGE_GENERATOR_OUTPUT_DIR:-/Users/robertgrzesik/Development/.image_generator_output}"
export IMAGE_GENERATOR_CALLER="${IMAGE_GENERATOR_CALLER:-creative-image-generator}"
export IMAGE_GENERATOR_MONTHLY_BUDGET_USD="${IMAGE_GENERATOR_MONTHLY_BUDGET_USD:-100}"

cd "$repo_dir"
exec /Users/robertgrzesik/.local/bin/uv run python server.py
