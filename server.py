#!/usr/bin/env python3
"""Provider-neutral, cost-controlled MCP image generator.

The active provider/model, output tier, and monthly budget are server settings.
Callers may supply only creative intent: prompt, purpose, aspect ratio, and
optional references. Gemini Pro, 2K/4K, auto quality, and per-call model
selection are deliberately unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from google import genai
from google.genai import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

APPROVED_MODEL = "gemini-3.1-flash-image-preview"
APPROVED_IMAGE_SIZE = "1K"
ESTIMATED_COST_USD = 0.067
PURPOSES = ("thumbnail", "slide", "email", "sms", "website", "test")
ASPECT_RATIOS = ("16:9", "1:1", "9:16")

OUTPUT_DIR = Path(
    os.environ.get(
        "IMAGE_GENERATOR_OUTPUT_DIR",
        str(Path.home() / "Documents" / "Development" / ".image_generator_output"),
    )
).expanduser().resolve()
STATE_DIR = Path(
    os.environ.get(
        "IMAGE_GENERATOR_STATE_DIR",
        str(Path.home() / ".local" / "state" / "image-generator"),
    )
).expanduser().resolve()
LEDGER_PATH = STATE_DIR / "usage.sqlite3"
CALLER = os.environ.get("IMAGE_GENERATOR_CALLER", "creative-image-generator")
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _monthly_budget_usd() -> float:
    value = float(os.environ.get("IMAGE_GENERATOR_MONTHLY_BUDGET_USD", "100"))
    if value < 100 or value % 100 != 0:
        raise RuntimeError(
            "IMAGE_GENERATOR_MONTHLY_BUDGET_USD must be a positive $100 increment."
        )
    return value


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured for this server.")
    return key


def _key_id() -> str:
    return hashlib.sha256(_api_key().encode()).hexdigest()[:12]


def _connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            month TEXT NOT NULL,
            purpose TEXT NOT NULL,
            caller TEXT NOT NULL,
            key_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            resolution TEXT NOT NULL,
            aspect_ratio TEXT NOT NULL,
            retries INTEGER NOT NULL,
            estimated_cost_usd REAL NOT NULL,
            actual_cost_usd REAL,
            elapsed_ms INTEGER,
            status TEXT NOT NULL,
            error_type TEXT
        )
        """
    )
    return conn


def _month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _reserve(purpose: str, aspect_ratio: str) -> int:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"aspect_ratio must be one of {ASPECT_RATIOS}")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        spent = float(
            conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM usage "
                "WHERE month = ? AND status IN ('reserved', 'succeeded', 'failed')",
                (_month(),),
            ).fetchone()[0]
        )
        budget = _monthly_budget_usd()
        if spent + ESTIMATED_COST_USD > budget:
            raise RuntimeError(
                f"Creative image budget exhausted: ${spent:.2f} used/reserved of "
                f"${budget:.2f} for {_month()}. Rob must explicitly raise the "
                "server budget by another $100 increment."
            )
        cursor = conn.execute(
            """
            INSERT INTO usage (
                created_at, month, purpose, caller, key_id, provider, model,
                resolution, aspect_ratio, retries, estimated_cost_usd, status
            ) VALUES (?, ?, ?, ?, ?, 'google', ?, ?, ?, 0, ?, 'reserved')
            """,
            (
                datetime.now(UTC).isoformat(),
                _month(),
                purpose,
                CALLER,
                _key_id(),
                APPROVED_MODEL,
                APPROVED_IMAGE_SIZE,
                aspect_ratio,
                ESTIMATED_COST_USD,
            ),
        )
        return int(cursor.lastrowid)


def _finish(reservation_id: int, *, elapsed_ms: int, error: Exception | None) -> None:
    with _connect() as conn:
        if error is None:
            conn.execute(
                "UPDATE usage SET status='succeeded', actual_cost_usd=?, "
                "elapsed_ms=? WHERE id=?",
                (ESTIMATED_COST_USD, elapsed_ms, reservation_id),
            )
        else:
            conn.execute(
                "UPDATE usage SET status='failed', actual_cost_usd=?, elapsed_ms=?, "
                "error_type=? WHERE id=?",
                (ESTIMATED_COST_USD, elapsed_ms, type(error).__name__, reservation_id),
            )


def _usage_report() -> dict[str, object]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT purpose, model, resolution, COUNT(*),
                   ROUND(SUM(estimated_cost_usd), 4),
                   ROUND(SUM(COALESCE(actual_cost_usd, 0)), 4)
            FROM usage WHERE month=?
            GROUP BY purpose, model, resolution
            ORDER BY purpose, model
            """,
            (_month(),),
        ).fetchall()
    return {
        "month": _month(),
        "budget_usd": _monthly_budget_usd(),
        "groups": [
            {
                "purpose": row[0],
                "model": row[1],
                "resolution": row[2],
                "requests": row[3],
                "estimated_cost_usd": row[4],
                "actual_cost_usd": row[5],
            }
            for row in rows
        ],
    }


def _read_reference(path_str: str) -> types.Part:
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Reference image not found: {path}")
    mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if not mime:
        raise ValueError(f"Unsupported reference image type: {path.suffix}")
    data = path.read_bytes()
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError(f"Reference image exceeds {MAX_REFERENCE_BYTES} bytes")
    return types.Part.from_bytes(data=data, mime_type=mime)


def _extract_images(response: object) -> list[tuple[bytes, str]]:
    found: list[tuple[bytes, str]] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                found.append((data, getattr(inline, "mime_type", "image/png")))
    return found


def _extension(mime: str) -> str:
    return {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")


def _generate(
    *, prompt: str, purpose: str, aspect_ratio: str, references: list[str]
) -> list[Path]:
    reservation_id = _reserve(purpose, aspect_ratio)
    started = time.monotonic()
    error: Exception | None = None
    try:
        contents: list[object] = [prompt]
        contents.extend(_read_reference(path) for path in references)
        response = genai.Client(api_key=_api_key()).models.generate_content(
            model=APPROVED_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio=aspect_ratio,
                    image_size=APPROVED_IMAGE_SIZE,
                ),
            ),
        )
        images = _extract_images(response)
        if not images:
            raise RuntimeError("Approved generator returned no image")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        for index, (data, mime) in enumerate(images, start=1):
            path = OUTPUT_DIR / f"image_{stamp}_{index}.{_extension(mime)}"
            path.write_bytes(data)
            paths.append(path)
        return paths
    except Exception as exc:
        error = exc
        raise
    finally:
        _finish(
            reservation_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )


server: Server = Server("image-generator")


@server.list_tools()
async def list_tools() -> list[Tool]:
    creative = {
        "prompt": {"type": "string", "description": "Complete image prompt."},
        "purpose": {
            "type": "string",
            "enum": list(PURPOSES),
            "description": "Required cost-attribution category.",
        },
        "aspect_ratio": {
            "type": "string",
            "enum": list(ASPECT_RATIOS),
            "description": "Creative aspect ratio; resolution remains server-controlled at 1K.",
        },
    }
    return [
        Tool(
            name="generate_image",
            description=(
                "Generate one image using the approved server-controlled provider, "
                "model, quality, and 1K resolution. Callers cannot select Pro, 2K, "
                "4K, or auto quality. The raw provider result is saved without edits."
            ),
            inputSchema={
                "type": "object",
                "properties": creative,
                "required": ["prompt", "purpose", "aspect_ratio"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="edit_image",
            description=(
                "Regenerate an image from references using the same approved, "
                "server-controlled 1K generator. The result is not hand-repaired."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **creative,
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 14,
                    },
                },
                "required": ["prompt", "purpose", "aspect_ratio", "reference_images"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="usage_report",
            description="Return the current calendar-month creative usage by purpose.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "usage_report":
            return [TextContent(type="text", text=json.dumps(_usage_report(), indent=2))]
        if name not in {"generate_image", "edit_image"}:
            raise ValueError(f"Unknown tool: {name}")
        paths = await asyncio.to_thread(
            _generate,
            prompt=str(arguments["prompt"]),
            purpose=str(arguments["purpose"]),
            aspect_ratio=str(arguments["aspect_ratio"]),
            references=list(arguments.get("reference_images") or []),
        )
        text = "Generated raw approved-provider image(s):\n" + "\n".join(
            f"- {path}" for path in paths
        )
        return [TextContent(type="text", text=text)]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {type(exc).__name__}: {exc}")]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
