#!/usr/bin/env python3
"""Prove, against the live provider, exactly which model / quality / size
combinations actually work. Run this instead of guessing.

Rob, 2026-09-13: "Can you use both flare and sunburst on all the different
levels, low to max, and all the different aspect ratios and everything else?
Is that all fully working? Please make sure it's fully working before I
fucking have to restart you again."

Every previously shipped breakage in this server was a config value that had
never been executed once: quality stuck on low, and a 1.91:1 size of 1536x804
that the provider rejects because it is not divisible by 16. Both would have
been caught by one run of this file.

Sizes are probed at `low` so the sweep stays cheap. Quality is probed once per
model at 1024x1024. Invalid sizes fail parameter validation before any image
is produced, so they cost nothing.

  python3 tests/provider_matrix_check.py            # sizes only, ~$0.15
  python3 tests/provider_matrix_check.py --full     # sizes + qualities, ~$1.10
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
# The five the server exposes, plus ratios worth having that it does not.
CANDIDATE_SIZES = {
    "16:9": "1536x864",
    "1:1": "1024x1024",
    "9:16": "864x1536",
    "1.91:1": "1536x800",
    "4:5": "1024x1280",
    "4:1 (PMax landscape logo slot)": "1536x384",
    "2:3 (Pinterest)": "1024x1536",
    "3:2": "1536x1024",
}
PROMPT = "A plain grey square. Nothing else."


def probe(client, model: str, size: str, quality: str) -> tuple[bool, str]:
    try:
        client.images.generate(
            model=model, prompt=PROMPT, size=size, quality=quality, n=1
        )
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - we are cataloguing provider errors
        message = str(exc)
        for marker in ("'message': '", '"message": "'):
            if marker in message:
                message = message.split(marker, 1)[1].split("'", 1)[0].split('"', 1)[0]
                break
        return False, message[:120]


def main() -> int:
    full = "--full" in sys.argv
    client = server.OpenAI(api_key=server._api_key())

    print("=== models the key can actually see ===")
    try:
        visible = sorted(
            m.id for m in client.models.list() if "image" in m.id
        )
        for model_id in visible:
            print(f"  {model_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"  models.list() failed: {exc}")
        visible = []
    print("  NOTE: models.list() has lied before. The probes below are the truth.")

    failures = []

    print("\n=== SIZE x MODEL (quality=low) ===")
    print(f"{'ratio':34} {'size':12} {'flare':10} {'sunburst':10}")
    for ratio, size in CANDIDATE_SIZES.items():
        row = []
        for model in MODELS:
            ok, why = probe(client, model, size, "low")
            row.append("ok" if ok else "FAIL")
            if not ok:
                failures.append((f"{model} {ratio} {size} low", why))
        approved = " (approved)" if ratio in server.APPROVED_SIZES else ""
        print(f"{ratio + approved:34} {size:12} {row[0]:10} {row[1]:10}")

    if full:
        print("\n=== QUALITY x MODEL (size=1024x1024) ===")
        print(f"{'quality':12} {'flare':10} {'sunburst':10}")
        for quality in server.ALLOWED_QUALITIES:
            row = []
            for model in MODELS:
                ok, why = probe(client, model, "1024x1024", quality)
                row.append("ok" if ok else "FAIL")
                if not ok:
                    failures.append((f"{model} 1024x1024 {quality}", why))
            print(f"{quality:12} {row[0]:10} {row[1]:10}")
    else:
        print("\n(skipping quality sweep; pass --full to run it)")

    print("\n=== FAILURES ===")
    if not failures:
        print("  none")
    for what, why in failures:
        print(f"  {what}\n      {why}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
