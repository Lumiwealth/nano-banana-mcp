#!/usr/bin/env python3
"""Provider-neutral command-line entrypoint for the approved Image Generator."""

from __future__ import annotations

import argparse
import json

import server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--purpose", required=True, choices=server.PURPOSES)
    parser.add_argument("--aspect-ratio", required=True, choices=server.ASPECT_RATIOS)
    parser.add_argument(
        "--quality",
        choices=server.ALLOWED_QUALITIES,
        default=server.DEFAULT_QUALITY,
    )
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--max-sdk-retries", type=int, choices=(0, 1, 2), default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = server._generate(
        prompt=args.prompt,
        purpose=args.purpose,
        aspect_ratio=args.aspect_ratio,
        quality=args.quality,
        references=args.reference,
        max_sdk_retries=args.max_sdk_retries,
    )
    print(
        json.dumps(
            {
                "paths": [str(path) for path in paths],
                "provider": "openai",
                "model": server.APPROVED_MODEL,
                "quality": args.quality,
                "resolution": server.APPROVED_SIZES[args.aspect_ratio],
                "purpose": args.purpose,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
