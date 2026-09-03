from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def test_schema_forbids_model_resolution_and_unbounded_quality_overrides() -> None:
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["generate_image"].inputSchema
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "prompt",
        "purpose",
        "aspect_ratio",
        "quality",
    }
    assert schema["properties"]["quality"]["enum"] == ["low", "medium"]
    assert schema["properties"]["quality"]["default"] == "low"
    assert "model" not in schema["properties"]
    assert "resolution" not in schema["properties"]
    assert "gemini-3-pro-image-preview" not in str(schema)
    assert "high" not in schema["properties"]["quality"]["enum"]
    assert "auto" not in schema["properties"]["quality"]["enum"]


def test_only_approved_gpt_image_model_and_fixed_sizes_exist() -> None:
    assert server.APPROVED_MODEL == "gpt-image-2"
    assert server.DEFAULT_QUALITY == "low"
    assert server.ALLOWED_QUALITIES == ("low", "medium")
    assert server.APPROVED_SIZES == {
        "16:9": "1536x864",
        "1:1": "1024x1024",
        "9:16": "864x1536",
    }
    assert "canonical name is Image Generator" in server.SERVER_INSTRUCTIONS
    assert "Nano Banana" in server.SERVER_INSTRUCTIONS
    assert "not as permission to select Google or Gemini" in server.SERVER_INSTRUCTIONS


def test_budget_rejects_request_over_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setattr(
        server, "ESTIMATED_COST_USD_BY_QUALITY", {"low": 100.01, "medium": 100.01}
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="budget exhausted"):
        server._reserve("slide", "16:9")


def test_ledger_attributes_without_prompt_or_raw_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setenv("OPENAI_API_KEY", "raw-secret-value")
    reservation_id = server._reserve("thumbnail", "16:9")
    server._finish(
        reservation_id, elapsed_ms=123, actual_cost_usd=0.0058, error=None
    )
    with sqlite3.connect(server.LEDGER_PATH) as conn:
        row = conn.execute(
            "SELECT purpose, caller, key_id, provider, model, quality, resolution, "
            "retries, actual_cost_usd, status FROM usage"
        ).fetchone()
        columns = {column[1] for column in conn.execute("PRAGMA table_info(usage)")}
    assert row[0] == "thumbnail"
    assert row[2] != "raw-secret-value"
    assert row[3] == "openai"
    assert row[4] == server.APPROVED_MODEL
    assert row[5] == "low"
    assert row[6] == "1536x864"
    assert row[7] == 0
    assert row[8] == 0.0058
    assert row[9] == "succeeded"
    assert "prompt" not in columns
    assert "api_key" not in columns


def test_budget_must_stay_within_company_creative_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATOR_MONTHLY_BUDGET_USD", "400")
    with pytest.raises(RuntimeError, match="company creative ceiling"):
        server._monthly_budget_usd()


def test_each_hundred_dollar_alert_is_recorded_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setattr(
        server, "ESTIMATED_COST_USD_BY_QUALITY", {"low": 100.0, "medium": 100.0}
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    first = server._reserve("slide", "16:9")
    server._finish(first, elapsed_ms=1, actual_cost_usd=100.0, error=None)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        server._reserve("slide", "16:9")
    with server._connect() as conn:
        alerts = conn.execute(
            "SELECT threshold_usd FROM spend_alerts ORDER BY threshold_usd"
        ).fetchall()
    assert alerts == [(100,)]
    assert len((tmp_path / "spend-alerts.jsonl").read_text().splitlines()) == 1


def test_actual_cost_uses_gpt_image_2_token_rates() -> None:
    class Response:
        def model_dump(self, **_kwargs):
            return {
                "usage": {
                    "input_tokens_details": {"text_tokens": 215, "image_tokens": 0},
                    "output_tokens_details": {"image_tokens": 158},
                }
            }

    assert server._actual_cost(Response()) == 0.005815


def test_medium_is_recorded_but_high_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    reservation_id = server._reserve("slide", "16:9", "medium")
    with server._connect() as conn:
        quality, estimate = conn.execute(
            "SELECT quality, estimated_cost_usd FROM usage WHERE id=?",
            (reservation_id,),
        ).fetchone()
    assert quality == "medium"
    assert estimate == 0.05
    with pytest.raises(ValueError, match="quality must be"):
        server._reserve("slide", "16:9", "high")
