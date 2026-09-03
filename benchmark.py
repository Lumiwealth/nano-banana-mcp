#!/usr/bin/env python3
"""Run a tiny, no-retry, cost-capped blind image benchmark.

This is an operator-only benchmark harness, not an MCP tool. Provider/model
selection is intentionally absent from the agent-facing server schema.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

import httpx
from openai import OpenAI
from PIL import Image

PROFILES: dict[str, dict[str, object]] = {
    "openai-gpt-image-2-low": {
        "provider": "openai", "model": "gpt-image-2", "quality": "low", "max_cost": 0.03,
    },
    "openai-gpt-image-2-medium": {
        "provider": "openai", "model": "gpt-image-2", "quality": "medium", "max_cost": 0.10,
    },
    "xai-grok-imagine-2-low": {
        "provider": "xai", "model": "grok-imagine-image-2.0", "quality": "low", "max_cost": 0.04,
    },
    "together-flux-2-dev": {
        "provider": "together", "model": "black-forest-labs/FLUX.2-dev", "max_cost": 0.0154,
    },
    "together-flux-2-pro": {
        "provider": "together", "model": "black-forest-labs/FLUX.2-pro", "max_cost": 0.03,
    },
    "together-qwen-image-2": {
        "provider": "together", "model": "Qwen/Qwen-Image-2.0", "max_cost": 0.035,
    },
    "together-gemini-flash": {
        "provider": "together", "model": "google/flash-image-3.1", "max_cost": 0.05,
    },
}

OPENAI_TEXT_INPUT_USD_PER_MILLION = 5.0
OPENAI_IMAGE_INPUT_USD_PER_MILLION = 8.0
OPENAI_IMAGE_OUTPUT_USD_PER_MILLION = 30.0


def _response_bytes(item: object) -> bytes:
    b64 = getattr(item, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None)
    if not url:
        raise RuntimeError("Provider response contained neither image bytes nor URL")
    with urlopen(url, timeout=120) as response:  # noqa: S310 - provider URL
        return response.read()


def _openai_generate(profile: dict[str, object], prompt: str) -> tuple[bytes, dict]:
    response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).images.generate(
        model=str(profile["model"]),
        prompt=prompt,
        quality=str(profile["quality"]),
        size="1536x1024",
        output_format="png",
    )
    return _response_bytes(response.data[0]), response.model_dump(exclude={"data"})


def _xai_generate(profile: dict[str, object], prompt: str) -> tuple[bytes, dict]:
    response = OpenAI(
        api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1"
    ).images.generate(
        model=str(profile["model"]),
        prompt=prompt,
        response_format="b64_json",
        extra_body={"quality": "low", "aspect_ratio": "16:9", "resolution": "1k"},
    )
    return _response_bytes(response.data[0]), response.model_dump(exclude={"data"})


def _together_generate(profile: dict[str, object], prompt: str) -> tuple[bytes, dict]:
    request = {
        "model": profile["model"],
        "prompt": prompt,
        "n": 1,
        "response_format": "base64",
        "output_format": "png",
    }
    if str(profile["model"]).startswith("google/"):
        # Together's Gemini 3.1 Flash endpoint requires one of Google's exact
        # supported dimensions; a generic aspect_ratio silently returned 1:1.
        request.update({"width": 1376, "height": 768})
    else:
        request.update({"width": 1344, "height": 768})
    response = httpx.post(
        "https://api.together.xyz/v1/images/generations",
        headers={"Authorization": f"Bearer {os.environ['TOGETHER_API_KEY']}"},
        json=request,
        timeout=180,
    )
    if response.is_error:
        raise RuntimeError(
            f"Together returned HTTP {response.status_code}: {response.text[:1000]}"
        )
    payload = response.json()
    item = payload["data"][0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        with urlopen(item["url"], timeout=120) as downloaded:  # noqa: S310
            data = downloaded.read()
    else:
        raise RuntimeError("Together response contained no image")
    return data, {key: payload.get(key) for key in ("id", "model", "request_id") if payload.get(key)}


def _generate(profile: dict[str, object], prompt: str) -> tuple[bytes, dict]:
    provider = profile["provider"]
    if provider == "openai":
        return _openai_generate(profile, prompt)
    if provider == "xai":
        return _xai_generate(profile, prompt)
    if provider == "together":
        return _together_generate(profile, prompt)
    raise ValueError(f"Unsupported provider: {provider}")


def _actual_cost(profile: dict[str, object], metadata: dict) -> float:
    """Return provider-reported or official-price-calculated request cost."""
    if profile["provider"] == "openai":
        usage = metadata.get("usage") or {}
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        text_input = int(input_details.get("text_tokens") or 0)
        image_input = int(input_details.get("image_tokens") or 0)
        image_output = int(output_details.get("image_tokens") or usage.get("output_tokens") or 0)
        return round(
            (
                text_input * OPENAI_TEXT_INPUT_USD_PER_MILLION
                + image_input * OPENAI_IMAGE_INPUT_USD_PER_MILLION
                + image_output * OPENAI_IMAGE_OUTPUT_USD_PER_MILLION
            )
            / 1_000_000,
            6,
        )
    if profile["provider"] == "xai":
        ticks = ((metadata.get("usage") or {}).get("cost_in_usd_ticks"))
        if ticks is not None:
            return round(float(ticks) / 10_000_000_000, 6)
    return round(float(profile["max_cost"]), 6)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-spend", type=float, default=2.0)
    parser.add_argument("--profiles", nargs="+", choices=sorted(PROFILES), default=sorted(PROFILES))
    parser.add_argument("--prompt-ids", nargs="+", default=None)
    args = parser.parse_args()
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    selected_prompts = {
        prompt_id: prompt
        for prompt_id, prompt in prompts.items()
        if args.prompt_ids is None or prompt_id in args.prompt_ids
    }
    if args.prompt_ids is not None and set(args.prompt_ids) != set(selected_prompts):
        raise RuntimeError("One or more requested prompt IDs do not exist")
    jobs = [(prompt_id, prompt, name) for prompt_id, prompt in selected_prompts.items() for name in args.profiles]
    maximum = sum(float(PROFILES[name]["max_cost"]) for _, _, name in jobs)
    if maximum > args.max_spend:
        raise RuntimeError(f"Refusing benchmark: maximum ${maximum:.4f} exceeds ${args.max_spend:.2f}")

    missing = sorted(
        {
            {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY", "together": "TOGETHER_API_KEY"}[
                str(PROFILES[name]["provider"])
            ]
            for _, _, name in jobs
            if not os.environ.get(
                {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY", "together": "TOGETHER_API_KEY"}[
                    str(PROFILES[name]["provider"])
                ]
            )
        }
    )
    if missing:
        raise RuntimeError(f"Missing required environment variables: {missing}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    randomizer = random.Random("botspot-blind-image-benchmark-2026-09-03")
    shuffled = jobs[:]
    randomizer.shuffle(shuffled)
    labels = {job: f"{chr(65 + index // 26)}{chr(65 + index % 26)}" for index, job in enumerate(shuffled)}
    public_path = args.output_dir / "blind-index.json"
    private_path = args.output_dir / ".private-mapping.json"
    public: list[dict] = (
        json.loads(public_path.read_text(encoding="utf-8")) if public_path.exists() else []
    )
    private: list[dict] = (
        json.loads(private_path.read_text(encoding="utf-8")) if private_path.exists() else []
    )
    completed = {(row["prompt_id"], row["model"]) for row in private}
    spent = sum(float(row["maximum_cost_usd"]) for row in private)
    actual_spent = sum(float(row["actual_cost_usd"]) for row in private)
    for prompt_id, prompt, profile_name in jobs:
        profile = PROFILES[profile_name]
        if (prompt_id, profile["model"]) in completed:
            continue
        estimated = float(profile["max_cost"])
        if spent + estimated > args.max_spend:
            raise RuntimeError("Runtime spend ceiling would be exceeded")
        started = time.monotonic()
        data, metadata = _generate(profile, prompt)
        elapsed = round(time.monotonic() - started, 3)
        spent += estimated
        actual = _actual_cost(profile, metadata)
        actual_spent += actual
        label = labels[(prompt_id, prompt, profile_name)]
        output = args.output_dir / f"{prompt_id}-{label}.png"
        output.write_bytes(data)
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
        common = {
            "prompt_id": prompt_id,
            "label": label,
            "file": output.name,
            "width": width,
            "height": height,
            "elapsed_seconds": elapsed,
            "retries": 0,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        public.append(common)
        private.append(
            {
                **common,
                "provider": profile["provider"],
                "model": profile["model"],
                "quality": profile.get("quality"),
                "maximum_cost_usd": estimated,
                "actual_cost_usd": actual,
                "response_metadata": metadata,
            }
        )
        public_path.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
        private_path.write_text(json.dumps(private, indent=2) + "\n", encoding="utf-8")
        private_path.chmod(0o600)

    receipt = {
        "created_at": datetime.now(UTC).isoformat(),
        "authorized_ceiling_usd": args.max_spend,
        "maximum_estimated_spend_usd": round(spent, 4),
        "actual_spend_usd": round(actual_spent, 6),
        "jobs": len(jobs),
        "retries": 0,
    }
    (args.output_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
