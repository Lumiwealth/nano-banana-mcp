from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark


def test_benchmark_catalog_stays_under_two_dollars_for_two_prompts() -> None:
    per_prompt = sum(float(profile["max_cost"]) for profile in benchmark.PROFILES.values())
    assert per_prompt * 2 < 2.0


def test_benchmark_has_no_pro_gemini_control_call() -> None:
    models = {str(profile["model"]) for profile in benchmark.PROFILES.values()}
    assert "gemini-3-pro-image-preview" not in models
    assert "gpt-image-2" in models
    assert "Qwen/Qwen-Image-2.0" in models


def test_openai_actual_cost_uses_official_token_rates() -> None:
    profile = benchmark.PROFILES["openai-gpt-image-2-low"]
    metadata = {
        "usage": {
            "input_tokens_details": {"text_tokens": 215, "image_tokens": 0},
            "output_tokens": 158,
            "output_tokens_details": {"image_tokens": 158},
        }
    }
    assert benchmark._actual_cost(profile, metadata) == 0.005815


def test_xai_actual_cost_uses_provider_ticks() -> None:
    profile = benchmark.PROFILES["xai-grok-imagine-2-low"]
    assert benchmark._actual_cost(
        profile, {"usage": {"cost_in_usd_ticks": 400_000_000}}
    ) == 0.04
