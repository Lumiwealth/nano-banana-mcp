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
import mimetypes
import os
import random
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

import httpx
from google import genai
from google.genai import types as genai_types
from openai import OpenAI
from PIL import Image

PROFILES: dict[str, dict[str, object]] = {
    "openai-gpt-image-2-low": {
        "provider": "openai", "model": "gpt-image-2", "quality": "low", "max_cost": 0.03,
    },
    "openai-gpt-image-2-medium": {
        "provider": "openai", "model": "gpt-image-2", "quality": "medium", "max_cost": 0.10,
    },
    "google-gemini-flash-lite-1k": {
        "provider": "google",
        "model": "gemini-3.1-flash-lite-image",
        "quality": "1K",
        "max_cost": 0.04,
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
GOOGLE_INPUT_USD_PER_MILLION = 0.25
GOOGLE_TEXT_OUTPUT_USD_PER_MILLION = 1.50
GOOGLE_IMAGE_OUTPUT_USD_PER_MILLION = 30.0


def _response_bytes(item: object) -> bytes:
    b64 = getattr(item, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None)
    if not url:
        raise RuntimeError("Provider response contained neither image bytes nor URL")
    with urlopen(url, timeout=120) as response:  # noqa: S310 - provider URL
        return response.read()


def _openai_generate(
    profile: dict[str, object],
    prompt: str,
    reference_images: tuple[Path, ...],
    aspect_ratio: str,
) -> tuple[bytes, dict]:
    sizes = {"16:9": "1536x864", "1:1": "1024x1024", "9:16": "864x1536"}
    if aspect_ratio not in sizes:
        raise ValueError(f"Unsupported aspect ratio: {aspect_ratio}")
    common = {
        "model": str(profile["model"]),
        "prompt": prompt,
        "quality": str(profile["quality"]),
        "size": sizes[aspect_ratio],
        "output_format": "png",
    }
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    if reference_images:
        with ExitStack() as stack:
            files = [stack.enter_context(path.open("rb")) for path in reference_images]
            response = client.images.edit(image=files, **common)
    else:
        response = client.images.generate(**common)
    return _response_bytes(response.data[0]), response.model_dump(exclude={"data"})


def _google_generate(
    profile: dict[str, object],
    prompt: str,
    reference_images: tuple[Path, ...],
    aspect_ratio: str,
) -> tuple[bytes, dict]:
    contents: list[object] = []
    for path in reference_images:
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        contents.append(
            genai_types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)
        )
    contents.append(prompt)
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    response = client.models.generate_content(
        model=str(profile["model"]),
        contents=contents,
        config=genai_types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=genai_types.ImageConfig(
                aspect_ratio=aspect_ratio, image_size="1K"
            ),
        ),
    )
    parts = response.candidates[0].content.parts if response.candidates else []
    for part in parts:
        if part.inline_data and part.inline_data.data:
            usage = response.usage_metadata.model_dump() if response.usage_metadata else {}
            return bytes(part.inline_data.data), {"usage": usage}
    raise RuntimeError("Google returned no image data")


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


def _generate(
    profile: dict[str, object],
    prompt: str,
    reference_images: tuple[Path, ...] = (),
    aspect_ratio: str = "16:9",
) -> tuple[bytes, dict]:
    provider = profile["provider"]
    if provider == "openai":
        return _openai_generate(profile, prompt, reference_images, aspect_ratio)
    if provider == "google":
        return _google_generate(profile, prompt, reference_images, aspect_ratio)
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
    if profile["provider"] == "google":
        usage = metadata.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_token_count") or 0)
        candidate_tokens = int(usage.get("candidates_token_count") or 0)
        image_output_tokens = sum(
            int(detail.get("token_count") or 0)
            for detail in usage.get("candidates_tokens_details") or []
            if str(detail.get("modality") or "").upper().split(".")[-1] == "IMAGE"
        )
        text_output_tokens = max(0, candidate_tokens - image_output_tokens)
        return round(
            (
                prompt_tokens * GOOGLE_INPUT_USD_PER_MILLION
                + text_output_tokens * GOOGLE_TEXT_OUTPUT_USD_PER_MILLION
                + image_output_tokens * GOOGLE_IMAGE_OUTPUT_USD_PER_MILLION
            )
            / 1_000_000,
            6,
        )
    return round(float(profile["max_cost"]), 6)


def _provider_label(profile: dict[str, object]) -> str:
    if profile["provider"] == "openai":
        return "GPT IMAGE 2 LOW"
    if profile["provider"] == "google":
        return "GEMINI FLASH LITE 1K"
    return str(profile["model"]).upper()


def _labeled_prompt(prompt: str, profile: dict[str, object]) -> str:
    return (
        f"{prompt}\n\n"
        "Required comparison label: add one small, clean, unobtrusive badge in the "
        f'bottom-right corner containing exactly: "{_provider_label(profile)}". '
        "This badge is allowed in addition to the requested headline. Do not add any "
        "other words. Render the badge as an integrated part of the generated image."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-spend", type=float, default=2.0)
    parser.add_argument("--profiles", nargs="+", choices=sorted(PROFILES), default=sorted(PROFILES))
    parser.add_argument("--prompt-ids", nargs="+", default=None)
    parser.add_argument(
        "--no-embedded-label",
        action="store_true",
        help="Do not ask providers to render a comparison badge inside the image.",
    )
    args = parser.parse_args()
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    selected_prompts = {
        prompt_id: (
            prompt if isinstance(prompt, dict) else {"prompt": prompt, "reference_images": []}
        )
        for prompt_id, prompt in prompts.items()
        if args.prompt_ids is None or prompt_id in args.prompt_ids
    }
    if args.prompt_ids is not None and set(args.prompt_ids) != set(selected_prompts):
        raise RuntimeError("One or more requested prompt IDs do not exist")
    jobs = [
        (
            prompt_id,
            str(prompt["prompt"]),
            tuple(Path(path).expanduser().resolve() for path in prompt.get("reference_images", [])),
            str(prompt.get("aspect_ratio", "16:9")),
            name,
        )
        for prompt_id, prompt in selected_prompts.items()
        for name in args.profiles
    ]
    missing_references = sorted(
        str(path)
        for _, _, references, _, _ in jobs
        for path in references
        if not path.is_file()
    )
    if missing_references:
        raise RuntimeError(f"Reference images do not exist: {missing_references}")
    maximum = sum(float(PROFILES[name]["max_cost"]) for _, _, _, _, name in jobs)
    if maximum > args.max_spend:
        raise RuntimeError(f"Refusing benchmark: maximum ${maximum:.4f} exceeds ${args.max_spend:.2f}")

    missing = sorted(
        {
            {"openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY", "together": "TOGETHER_API_KEY"}[
                str(PROFILES[name]["provider"])
            ]
            for _, _, _, _, name in jobs
            if not os.environ.get(
                {"openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY", "together": "TOGETHER_API_KEY"}[
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
    # Recalculate from stored provider usage on resume so pricing fixes repair
    # the local ledger without repeating paid generations.
    for row in private:
        matching_profile = next(
            (
                profile
                for profile in PROFILES.values()
                if profile["provider"] == row.get("provider")
                and profile["model"] == row.get("model")
                and profile.get("quality") == row.get("quality")
            ),
            None,
        )
        if matching_profile is not None:
            row["actual_cost_usd"] = _actual_cost(
                matching_profile, row.get("response_metadata") or {}
            )
    completed = {(row["prompt_id"], row["model"]) for row in private}
    spent = sum(float(row["maximum_cost_usd"]) for row in private)
    actual_spent = sum(float(row["actual_cost_usd"]) for row in private)
    for prompt_id, prompt, reference_images, aspect_ratio, profile_name in jobs:
        profile = PROFILES[profile_name]
        if (prompt_id, profile["model"]) in completed:
            continue
        estimated = float(profile["max_cost"])
        if spent + estimated > args.max_spend:
            raise RuntimeError("Runtime spend ceiling would be exceeded")
        started = time.monotonic()
        rendered_prompt = prompt if args.no_embedded_label else _labeled_prompt(prompt, profile)
        data, metadata = _generate(
            profile, rendered_prompt, reference_images, aspect_ratio
        )
        elapsed = round(time.monotonic() - started, 3)
        spent += estimated
        actual = _actual_cost(profile, metadata)
        actual_spent += actual
        label = labels[(prompt_id, prompt, reference_images, aspect_ratio, profile_name)]
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
            "aspect_ratio": aspect_ratio,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        public.append(common)
        private.append(
            {
                **common,
                "provider": profile["provider"],
                "model": profile["model"],
                "quality": profile.get("quality"),
                "reference_images": [str(path) for path in reference_images],
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
    private_path.write_text(json.dumps(private, indent=2) + "\n", encoding="utf-8")
    private_path.chmod(0o600)
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
