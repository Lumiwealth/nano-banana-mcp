"""run.sh must give the generator its own production creative key.

The BotSpot Agent development .env key is disabled on purpose (zero paid AI in
development, 2026-11-06). The image generator is approved production creative
work, so run.sh must export IMAGE_GENERATOR_OPENAI_API_KEY from a dedicated
key file when present. server.py reads that variable before OPENAI_API_KEY.
"""

import pathlib
import subprocess

RUN_SH = pathlib.Path(__file__).resolve().parents[1] / "run.sh"


def _exported_key(home: pathlib.Path) -> str:
    # Run run.sh up to (not including) the exec line, then print the key.
    script = RUN_SH.read_text()
    head = script.split("\ncd \"$repo_dir\"")[0]
    out = subprocess.run(
        ["bash", "-c", head + '\nprintf %s "${IMAGE_GENERATOR_OPENAI_API_KEY:-}"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_dedicated_key_file_wins(tmp_path):
    key_dir = tmp_path / ".config" / "image-generator"
    key_dir.mkdir(parents=True)
    (key_dir / "openai-api-key").write_text("sk-test-dedicated-image-key\n")
    assert _exported_key(tmp_path) == "sk-test-dedicated-image-key"


def test_server_prefers_image_generator_key():
    source = (RUN_SH.parent / "server.py").read_text()
    assert 'os.environ.get("IMAGE_GENERATOR_OPENAI_API_KEY") or os.environ.get(' in source
