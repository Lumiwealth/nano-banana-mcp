#!/usr/bin/env bash
set -euo pipefail

repo_dir="/Users/robertgrzesik/Development/mcp_servers/nano_banana"
credentials_file="/Users/robertgrzesik/Development/botspot_agent/.env"

if [[ ! -f "$credentials_file" ]]; then
  echo "Missing approved credential source: $credentials_file" >&2
  exit 1
fi

set -a
source "$credentials_file"
set +a

export IMAGE_GENERATOR_OUTPUT_DIR="${IMAGE_GENERATOR_OUTPUT_DIR:-/Users/robertgrzesik/Development/.image_generator_output}"
export IMAGE_GENERATOR_CALLER="${IMAGE_GENERATOR_CALLER:-creative-image-generator}"
export IMAGE_GENERATOR_MONTHLY_BUDGET_USD="${IMAGE_GENERATOR_MONTHLY_BUDGET_USD:-100}"

cd "$repo_dir"
exec /Users/robertgrzesik/.local/bin/uv run python server.py
