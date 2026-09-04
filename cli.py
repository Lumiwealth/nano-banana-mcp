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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = server._generate(
        prompt=args.prompt,
        purpose=args.purpose,
        aspect_ratio=args.aspect_ratio,
        quality=args.quality,
        references=args.reference,
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
