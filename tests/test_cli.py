from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli


def test_cli_locks_model_resolution_and_defaults_to_low(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "result.png"
    output.write_bytes(b"image")
    captured: dict[str, object] = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return [output]

    monkeypatch.setattr(cli.server, "_generate", fake_generate)
    assert cli.main(
        [
            "--prompt",
            "A thumbnail",
            "--purpose",
            "thumbnail",
            "--aspect-ratio",
            "16:9",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured == {
        "prompt": "A thumbnail",
        "purpose": "thumbnail",
        "aspect_ratio": "16:9",
        "quality": "low",
        "references": [],
    }
    assert payload["model"] == "gpt-image-2"
    assert payload["resolution"] == "1536x864"


def test_cli_has_no_model_resolution_or_high_quality_override() -> None:
    actions = {action.dest for action in cli._parser()._actions}
    assert "model" not in actions
    assert "resolution" not in actions
    quality = next(action for action in cli._parser()._actions if action.dest == "quality")
    assert tuple(quality.choices) == ("low", "medium")
