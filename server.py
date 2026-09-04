#!/usr/bin/env python3
"""Provider-neutral, cost-controlled MCP image generator.

The provider/model, resolution, and monthly budget are server settings. Callers
may supply creative intent plus one tightly bounded quality choice: low is the
default and medium is the only permitted upgrade. High/auto quality and
per-call model or resolution selection are deliberately unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from openai import OpenAI

APPROVED_MODEL = "gpt-image-2"
DEFAULT_QUALITY = "low"
ALLOWED_QUALITIES = ("low", "medium")
SERVER_INSTRUCTIONS = (
    "This server's canonical name is Image Generator. Treat user phrases such as "
    "'Nano Banana', 'nano-banana', or 'make an image' as image-generation intent, "
    "not as permission to select Google or Gemini. Always use this server's locked "
    "provider and model unless Rob explicitly requests a provider-specific exception."
)
APPROVED_SIZES = {
    "16:9": "1536x864",
    "1:1": "1024x1024",
    "9:16": "864x1536",
}
# Deliberately conservative reservation ceilings. Successful calls replace
# these with token-derived actual cost in the ledger.
ESTIMATED_COST_USD_BY_QUALITY = {"low": 0.01, "medium": 0.05}
OPENAI_TEXT_INPUT_USD_PER_MILLION = 5.0
OPENAI_IMAGE_INPUT_USD_PER_MILLION = 8.0
OPENAI_IMAGE_OUTPUT_USD_PER_MILLION = 30.0
COMPANY_CREATIVE_HARD_CEILING_USD = 300.0
ALERT_INTERVAL_USD = 100
DEFAULT_MAX_REQUESTS_PER_MINUTE = 10
PURPOSES = ("thumbnail", "slide", "email", "sms", "website", "test")
ASPECT_RATIOS = tuple(APPROVED_SIZES)

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
    if value < 100 or value % 100 != 0 or value > COMPANY_CREATIVE_HARD_CEILING_USD:
        raise RuntimeError(
            "IMAGE_GENERATOR_MONTHLY_BUDGET_USD must be a $100 increment from "
            "$100 through the $300 company creative ceiling."
        )
    return value


def _api_key() -> str:
    key = os.environ.get("IMAGE_GENERATOR_OPENAI_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured for this server.")
    return key


def _max_requests_per_minute() -> int:
    value = int(
        os.environ.get(
            "IMAGE_GENERATOR_MAX_REQUESTS_PER_MINUTE",
            str(DEFAULT_MAX_REQUESTS_PER_MINUTE),
        )
    )
    if value < 1 or value > 60:
        raise RuntimeError(
            "IMAGE_GENERATOR_MAX_REQUESTS_PER_MINUTE must be between 1 and 60."
        )
    return value


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
            quality TEXT NOT NULL,
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
    columns = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
    if "quality" not in columns:
        conn.execute(
            "ALTER TABLE usage ADD COLUMN quality TEXT NOT NULL DEFAULT 'legacy'"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spend_alerts (
            month TEXT NOT NULL,
            threshold_usd INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            projected_spend_usd REAL NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (month, threshold_usd)
        )
        """
    )
    return conn


def _month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _deliver_alert(payload: dict[str, object]) -> None:
    """Persist every alert locally and optionally deliver it to a webhook."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    alert_file = STATE_DIR / "spend-alerts.jsonl"
    with alert_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    if sys.platform == "darwin":
        threshold = int(payload.get("threshold_usd") or 0)
        status = str(payload.get("status") or "reached")
        subprocess.run(
            [
                "osascript",
                "-e",
                (
                    'display notification "Creative image spend '
                    f'${threshold} {status}." with title "Image Generator budget"'
                ),
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    webhook = os.environ.get("IMAGE_GENERATOR_ALERT_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    request = Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):  # noqa: S310 - operator-configured webhook
            pass
    except Exception:
        # The hard budget remains authoritative even when notification delivery fails.
        pass


def _record_due_alerts(
    conn: sqlite3.Connection, *, projected_spend: float, budget: float
) -> None:
    for threshold in range(
        ALERT_INTERVAL_USD,
        int(COMPANY_CREATIVE_HARD_CEILING_USD) + ALERT_INTERVAL_USD,
        ALERT_INTERVAL_USD,
    ):
        if projected_spend < threshold or threshold > budget:
            continue
        status = "blocked" if projected_spend > budget and threshold == int(budget) else "reached"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO spend_alerts (
                month, threshold_usd, created_at, projected_spend_usd, status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_month(), threshold, datetime.now(UTC).isoformat(), projected_spend, status),
        )
        if cursor.rowcount:
            _deliver_alert(
                {
                    "type": "creative_image_spend_threshold",
                    "month": _month(),
                    "threshold_usd": threshold,
                    "projected_spend_usd": round(projected_spend, 4),
                    "status": status,
                }
            )


def _reserve(purpose: str, aspect_ratio: str, quality: str = DEFAULT_QUALITY) -> int:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"aspect_ratio must be one of {ASPECT_RATIOS}")
    if quality not in ALLOWED_QUALITIES:
        raise ValueError(f"quality must be one of {ALLOWED_QUALITIES}")
    estimated_cost = ESTIMATED_COST_USD_BY_QUALITY[quality]
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        one_minute_ago = datetime.fromtimestamp(time.time() - 60, UTC).isoformat()
        recent_requests = int(
            conn.execute(
                "SELECT COUNT(*) FROM usage WHERE created_at >= ? "
                "AND status IN ('reserved', 'succeeded', 'failed')",
                (one_minute_ago,),
            ).fetchone()[0]
        )
        max_requests = _max_requests_per_minute()
        if recent_requests >= max_requests:
            raise RuntimeError(
                "Creative image burst limit reached: "
                f"{recent_requests} requests in the last minute; limit is {max_requests}."
            )
        spent = float(
            conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM usage "
                "WHERE month = ? AND status IN ('reserved', 'succeeded', 'failed')",
                (_month(),),
            ).fetchone()[0]
        )
        budget = _monthly_budget_usd()
        projected = spent + estimated_cost
        _record_due_alerts(conn, projected_spend=projected, budget=budget)
        if projected > budget:
            raise RuntimeError(
                f"Creative image budget exhausted: ${spent:.2f} used/reserved of "
                f"${budget:.2f} for {_month()}. Rob must explicitly raise the "
                "server budget by another $100 increment."
            )
        cursor = conn.execute(
            """
            INSERT INTO usage (
                created_at, month, purpose, caller, key_id, provider, model,
                quality, resolution, aspect_ratio, retries, estimated_cost_usd,
                status
            ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, ?, ?, 0, ?, 'reserved')
            """,
            (
                datetime.now(UTC).isoformat(),
                _month(),
                purpose,
                CALLER,
                _key_id(),
                APPROVED_MODEL,
                quality,
                APPROVED_SIZES[aspect_ratio],
                aspect_ratio,
                estimated_cost,
            ),
        )
        return int(cursor.lastrowid)


def _finish(
    reservation_id: int,
    *,
    elapsed_ms: int,
    actual_cost_usd: float | None,
    error: Exception | None,
) -> None:
    with _connect() as conn:
        if error is None:
            conn.execute(
                "UPDATE usage SET status='succeeded', actual_cost_usd=?, "
                "elapsed_ms=? WHERE id=?",
                (actual_cost_usd, elapsed_ms, reservation_id),
            )
        else:
            conn.execute(
                "UPDATE usage SET status='failed', actual_cost_usd="
                "COALESCE(?, estimated_cost_usd), elapsed_ms=?, "
                "error_type=? WHERE id=?",
                (actual_cost_usd, elapsed_ms, type(error).__name__, reservation_id),
            )


def _usage_report() -> dict[str, object]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT caller, key_id, provider, purpose, model, quality, resolution, COUNT(*),
                   ROUND(SUM(estimated_cost_usd), 4),
                   ROUND(SUM(COALESCE(actual_cost_usd, 0)), 4)
            FROM usage WHERE month=?
            GROUP BY caller, key_id, provider, purpose, model, quality, resolution
            ORDER BY caller, purpose, model, quality
            """,
            (_month(),),
        ).fetchall()
        alerts = conn.execute(
            """
            SELECT threshold_usd, created_at, projected_spend_usd, status
            FROM spend_alerts WHERE month=? ORDER BY threshold_usd
            """,
            (_month(),),
        ).fetchall()
    return {
        "month": _month(),
        "budget_usd": _monthly_budget_usd(),
        "groups": [
            {
                "caller": row[0],
                "key_id": row[1],
                "provider": row[2],
                "purpose": row[3],
                "model": row[4],
                "quality": row[5],
                "resolution": row[6],
                "requests": row[7],
                "estimated_cost_usd": row[8],
                "actual_cost_usd": row[9],
            }
            for row in rows
        ],
        "alerts": [
            {
                "threshold_usd": row[0],
                "created_at": row[1],
                "projected_spend_usd": row[2],
                "status": row[3],
            }
            for row in alerts
        ],
    }


def _read_reference(path_str: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Reference image not found: {path}")
    mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if not mime:
        raise ValueError(f"Unsupported reference image type: {path.suffix}")
    if path.stat().st_size > MAX_REFERENCE_BYTES:
        raise ValueError(f"Reference image exceeds {MAX_REFERENCE_BYTES} bytes")
    return path


def _actual_cost(response: object) -> float | None:
    metadata = response.model_dump(exclude={"data"})
    usage = metadata.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    text_input = int(input_details.get("text_tokens") or 0)
    image_input = int(input_details.get("image_tokens") or 0)
    image_output = int(
        output_details.get("image_tokens") or usage.get("output_tokens") or 0
    )
    if not any((text_input, image_input, image_output)):
        return None
    return round(
        (
            text_input * OPENAI_TEXT_INPUT_USD_PER_MILLION
            + image_input * OPENAI_IMAGE_INPUT_USD_PER_MILLION
            + image_output * OPENAI_IMAGE_OUTPUT_USD_PER_MILLION
        )
        / 1_000_000,
        6,
    )


def _generate(
    *,
    prompt: str,
    purpose: str,
    aspect_ratio: str,
    quality: str,
    references: list[str],
) -> list[Path]:
    reservation_id = _reserve(purpose, aspect_ratio, quality)
    started = time.monotonic()
    error: Exception | None = None
    actual_cost_usd: float | None = None
    try:
        client = OpenAI(api_key=_api_key())
        common = {
            "model": APPROVED_MODEL,
            "prompt": prompt,
            "quality": quality,
            "size": APPROVED_SIZES[aspect_ratio],
            "output_format": "png",
        }
        if references:
            paths = [_read_reference(path) for path in references]
            with ExitStack() as stack:
                files = [stack.enter_context(path.open("rb")) for path in paths]
                response = client.images.edit(image=files, **common)
        else:
            response = client.images.generate(**common)
        actual_cost_usd = _actual_cost(response)
        images = [
            base64.b64decode(item.b64_json)
            for item in response.data
            if getattr(item, "b64_json", None)
        ]
        if not images:
            raise RuntimeError("Approved generator returned no image")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_paths: list[Path] = []
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        for index, data in enumerate(images, start=1):
            path = OUTPUT_DIR / f"image_{stamp}_{index}.png"
            path.write_bytes(data)
            output_paths.append(path)
        return output_paths
    except Exception as exc:
        error = exc
        raise
    finally:
        _finish(
            reservation_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            actual_cost_usd=actual_cost_usd,
            error=error,
        )


server: Server = Server("image-generator", instructions=SERVER_INSTRUCTIONS)


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
            "description": "Creative aspect ratio; exact resolution remains server-controlled.",
        },
        "quality": {
            "type": "string",
            "enum": list(ALLOWED_QUALITIES),
            "default": DEFAULT_QUALITY,
            "description": (
                "Optional output quality. Omit for low. Use medium only when the "
                "user requests it or an inspected low-quality result is insufficient."
            ),
        },
    }
    return [
        Tool(
            name="generate_image",
            description=(
                "Generate one image using the approved server-controlled provider, "
                "GPT Image 2 model, and exact resolution. Quality defaults to low; "
                "medium is the only allowed upgrade. High/auto quality and model or "
                "resolution overrides are unavailable. The raw result is saved "
                "without edits."
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
                "server-controlled generator. Low is the default and medium is the "
                "only allowed upgrade. The result is not hand-repaired."
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
            quality=str(arguments.get("quality", DEFAULT_QUALITY)),
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
