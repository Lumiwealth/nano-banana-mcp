from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def test_schema_forbids_model_resolution_and_quality_overrides() -> None:
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["generate_image"].inputSchema
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"prompt", "purpose", "aspect_ratio"}
    assert "gemini-3-pro-image-preview" not in str(schema)
    assert "4K" not in str(schema["properties"])


def test_only_approved_flash_model_exists() -> None:
    assert server.APPROVED_MODEL == "gemini-3.1-flash-image-preview"
    assert "pro" not in server.APPROVED_MODEL.lower()
    assert server.APPROVED_IMAGE_SIZE == "1K"


def test_budget_rejects_request_over_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setattr(server, "ESTIMATED_COST_USD", 100.01)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="budget exhausted"):
        server._reserve("slide", "16:9")


def test_ledger_attributes_without_prompt_or_raw_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setenv("GEMINI_API_KEY", "raw-secret-value")
    reservation_id = server._reserve("thumbnail", "16:9")
    server._finish(reservation_id, elapsed_ms=123, error=None)
    with sqlite3.connect(server.LEDGER_PATH) as conn:
        row = conn.execute(
            "SELECT purpose, caller, key_id, model, resolution, retries, status FROM usage"
        ).fetchone()
        columns = {column[1] for column in conn.execute("PRAGMA table_info(usage)")}
    assert row[0] == "thumbnail"
    assert row[2] != "raw-secret-value"
    assert row[3] == server.APPROVED_MODEL
    assert row[4] == "1K"
    assert row[5] == 0
    assert row[6] == "succeeded"
    assert "prompt" not in columns
    assert "api_key" not in columns


def test_budget_must_stay_within_company_creative_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATOR_MONTHLY_BUDGET_USD", "400")
    with pytest.raises(RuntimeError, match="company creative ceiling"):
        server._monthly_budget_usd()


def test_each_hundred_dollar_alert_is_recorded_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "usage.sqlite3")
    monkeypatch.setattr(server, "ESTIMATED_COST_USD", 100.0)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    first = server._reserve("slide", "16:9")
    server._finish(first, elapsed_ms=1, error=None)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        server._reserve("slide", "16:9")
    with server._connect() as conn:
        alerts = conn.execute(
            "SELECT threshold_usd FROM spend_alerts ORDER BY threshold_usd"
        ).fetchall()
    assert alerts == [(100,)]
    assert len((tmp_path / "spend-alerts.jsonl").read_text().splitlines()) == 1
