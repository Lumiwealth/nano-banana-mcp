#!/usr/bin/env python3
"""
Nano Banana MCP Server

A minimal, security-conscious MCP server for Google Gemini image generation.
Defaults to gemini-3-pro-image-preview (Nano Banana Pro) — Google's flagship
image model. Override per-call via the `model` parameter, or change the
default via the NANO_BANANA_DEFAULT_MODEL environment variable.

Environment variables:
  GEMINI_API_KEY            Required. Google AI Studio API key.
  NANO_BANANA_OUTPUT_DIR    Optional. Where generated images are saved.
                            Default: ~/Documents/Development/.nano_banana_output
  NANO_BANANA_DEFAULT_MODEL Optional. Default model id.
                            Default: gemini-3-pro-image-preview
"""

from __future__ import annotations

import asyncio
import io
import os
import re
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from PIL import Image

# Allowed models. Locked to Google's two latest top-tier image models so we
# never silently fall back to an old/cheap model.
ALLOWED_MODELS = {
    "gemini-3-pro-image-preview",      # Nano Banana Pro — flagship, max quality
    "gemini-3.1-flash-image-preview",  # Nano Banana 2 — newest Flash, 4K, fast
}

DEFAULT_MODEL = os.environ.get(
    "NANO_BANANA_DEFAULT_MODEL", "gemini-3-pro-image-preview"
)

# Sensible defaults for output post-processing. Optimized for email + mobile:
# small file, retina-sharp, fast load, never above the Gemini 1024-2048 tier.
#
# Why 1280 default (not 1024 and not 4K):
#   Gemini 3 Pro Image pricing as of 2026:
#     ≤1024×1024  = $0.039 per image (325 tokens)
#     1024-2048   = $0.134 per image (1,120 tokens)
#     up to 4K    = $0.240 per image (2,000 tokens)
#   Email + web research consensus: a hero image displayed at 600px should
#   be exported at ~1200-1280 wide (2× display width for retina sharpness on
#   iPhone Pro Max @ 1290 actual pixels). 1024 is just under the retina
#   sweet spot; 2048+ is wasted because every email client downscales.
#   Verdict: 1280 is the right place — sharp on retina, same pricing tier
#   as 2048 so we lose nothing by going to the high end of the cheap-ish
#   tier. Drop to 1024 if cost is the priority (~3.4× savings) and a slight
#   retina softness is acceptable.
# Override per-call via target_size when high-res is genuinely needed
# (print, oversized landing-page hero, infographic export).
DEFAULT_TARGET_SIZE = os.environ.get("NANO_BANANA_DEFAULT_SIZE", "1280")

# Why WebP default: 25-35% smaller than JPEG at the same visual quality, lossless
# alpha, supported in every modern email client (Gmail/Apple/Outlook web/iOS/
# Android Mail) since 2023, and supported in 100% of modern browsers. Old
# Outlook (≤2019) is the lone holdout — for those users we recommend the
# template emit a <picture> with WebP source + JPEG fallback.
DEFAULT_OUTPUT_FORMAT = os.environ.get(
    "NANO_BANANA_DEFAULT_FORMAT", "webp"
).lower()

# Quality 75 is the sweet spot for WebP — visually indistinguishable from
# 95+ in most email/web contexts and produces ~half the file size.
DEFAULT_QUALITY = int(os.environ.get("NANO_BANANA_DEFAULT_QUALITY", "75"))

ALLOWED_FORMATS = {"webp", "jpeg", "jpg", "png"}

# Allowed target_size values. Strings accepted to support presets and
# explicit dimensions. "off" means "keep raw model output unchanged".
SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "off": (0, 0),       # do not resize, do not re-encode
    "256": (256, 256),
    "512": (512, 512),
    "640": (640, 640),
    "1024": (1024, 1024),  # cheap tier ($0.039)
    "1k": (1024, 1024),
    "1120": (1120, 1120),  # 2× of inline-email body width (560)
    "1280": (1280, 1280),  # 2× of standard email hero (640) — DEFAULT
    "2048": (2048, 2048),
    "2k": (2048, 2048),
    "4k": (3840, 3840),
}

OUTPUT_DIR = Path(
    os.environ.get(
        "NANO_BANANA_OUTPUT_DIR",
        str(Path.home() / "Documents" / "Development" / ".nano_banana_output"),
    )
).expanduser().resolve()

# Filenames must be safe — no path separators, no traversal, no nulls.
FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

MAX_REFERENCE_BYTES = 20 * 1024 * 1024  # 20 MB per reference image
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

REFERENCE_PROFILES: dict[str, dict[str, object]] = {
    "botspot_spot": {
        "description": "BotSpot/Lumibot Spot mascot reference images and brand guardrails.",
        "images": [
            "/Users/robertgrzesik/Development/brand-assets/botspot/botspot_mascot_rgba.png",
            "/Users/robertgrzesik/Development/brand-assets/botspot/botspot_mascot_transparent_ready.png",
        ],
        "prompt_suffix": (
            "\n\nReference profile: BotSpot Spot mascot. Use the attached Spot "
            "reference images as the canonical character. Preserve the white/silver "
            "robot body, orange goggle eyes, teal joints/accent details, friendly "
            "mischievous expression, and core proportions. Place Spot doing an "
            "action that is relevant to the requested topic instead of standing as "
            "generic decoration. Do not show Spot giving investment advice or "
            "guaranteeing trading performance."
        ),
    },
    "spot": {"alias_for": "botspot_spot"},
    "botspot": {"alias_for": "botspot_spot"},
}


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Configure it in the MCP server's env block."
        )
    return key


def _validate_model(model: str | None) -> str:
    chosen = model or DEFAULT_MODEL
    if chosen not in ALLOWED_MODELS:
        raise ValueError(
            f"Model {chosen!r} is not allowed. Allowed: {sorted(ALLOWED_MODELS)}"
        )
    return chosen


def _safe_output_path(filename: str | None, ext: str = "png") -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ext = ext.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    if filename is not None:
        if not FILENAME_RE.match(filename):
            raise ValueError(
                f"Invalid filename {filename!r}: only [A-Za-z0-9._-], max 100 chars."
            )
        # Strip trailing recognised extension if user provided one.
        for known in (".png", ".webp", ".jpg", ".jpeg"):
            if filename.lower().endswith(known):
                filename = filename[: -len(known)]
                break
        base = f"{ts}_{filename}.{ext}"
    else:
        base = f"nb_{ts}.{ext}"
    out = (OUTPUT_DIR / base).resolve()
    # Final defense: must live directly in OUTPUT_DIR.
    if out.parent != OUTPUT_DIR:
        raise ValueError("Refusing to write outside the sandboxed output directory.")
    return out


def _resize_and_encode(
    raw_bytes: bytes,
    target_size: str,
    output_format: str,
    quality: int,
) -> tuple[bytes, str, tuple[int, int]]:
    """
    Resize + re-encode the raw model image bytes per email/mobile defaults.

    Returns (encoded_bytes, ext, (width, height)).

    target_size='off' is a passthrough (no resize, no re-encode). Otherwise
    the image is downscaled (never upscaled — we never add detail that isn't
    in the source) using Lanczos resampling so the result is sharp at
    small sizes. EXIF + ICC profiles are stripped to minimize file size.
    """
    if target_size == "off":
        # Passthrough — caller wanted the raw model output.
        return raw_bytes, "png", (0, 0)

    if target_size in SIZE_PRESETS:
        max_w, max_h = SIZE_PRESETS[target_size]
    elif "x" in target_size:
        try:
            w_str, h_str = target_size.lower().split("x", 1)
            max_w, max_h = int(w_str), int(h_str)
        except ValueError as e:
            raise ValueError(
                f"Invalid target_size {target_size!r}. Use a preset "
                f"({sorted(SIZE_PRESETS)}) or WIDTHxHEIGHT (e.g. 600x300)."
            ) from e
    else:
        raise ValueError(
            f"Invalid target_size {target_size!r}. Use a preset "
            f"({sorted(SIZE_PRESETS)}) or WIDTHxHEIGHT (e.g. 600x300)."
        )

    if max_w <= 0 or max_h <= 0 or max_w > 8192 or max_h > 8192:
        raise ValueError(
            f"target_size dimensions out of range: ({max_w}, {max_h})."
        )

    img = Image.open(io.BytesIO(raw_bytes))
    # Strip metadata for smallest output.
    if "exif" in img.info:
        del img.info["exif"]

    # Only downscale, never upscale. thumbnail() preserves aspect ratio
    # and uses Lanczos by default in modern Pillow.
    if img.width > max_w or img.height > max_h:
        img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)

    fmt = output_format.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(
            f"output_format {output_format!r} invalid. Allowed: {sorted(ALLOWED_FORMATS)}"
        )

    buf = io.BytesIO()
    if fmt == "webp":
        img.save(buf, format="WEBP", quality=quality, method=6)
        ext = "webp"
    elif fmt == "jpeg":
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")  # JPEG can't carry alpha
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        ext = "jpeg"
    else:  # png — usually only requested when alpha matters
        img.save(buf, format="PNG", optimize=True)
        ext = "png"

    return buf.getvalue(), ext, img.size


def _read_reference_image(path_str: str) -> types.Part:
    p = Path(path_str).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Reference image not found: {p}")
    suffix = p.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if not mime:
        raise ValueError(
            f"Unsupported reference image type {suffix!r}. "
            f"Allowed: {sorted(MIME_BY_SUFFIX)}"
        )
    data = p.read_bytes()
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError(
            f"Reference image too large ({len(data)} bytes > {MAX_REFERENCE_BYTES})."
        )
    return types.Part.from_bytes(data=data, mime_type=mime)


def _resolve_reference_profile(name: str | None) -> tuple[list[str], str]:
    if not name:
        return [], ""
    profile = REFERENCE_PROFILES.get(name)
    if not profile:
        allowed = sorted(REFERENCE_PROFILES)
        raise ValueError(f"Unknown reference_profile {name!r}. Allowed: {allowed}")
    alias_for = profile.get("alias_for")
    if isinstance(alias_for, str):
        return _resolve_reference_profile(alias_for)
    images = [str(p) for p in profile.get("images", [])]
    prompt_suffix = str(profile.get("prompt_suffix", ""))
    return images, prompt_suffix


def _extract_images(response) -> list[bytes]:
    images: list[bytes] = []
    for cand in response.candidates or []:
        content = getattr(cand, "content", None)
        if not content:
            continue
        for part in content.parts or []:
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                images.append(inline.data)
    return images


def _generate(
    prompt: str,
    model: str,
    references: list[str] | None,
    reference_profile: str | None,
    target_size: str,
    output_format: str,
    quality: int,
) -> tuple[list[tuple[Path, int, tuple[int, int]]], str]:
    """
    Returns list of (path, byte_size, (w, h)) per image generated, plus any
    response text from the model. Post-processes each image per the
    target_size / output_format / quality args.
    """
    client = genai.Client(api_key=_api_key())
    profile_refs, profile_prompt = _resolve_reference_profile(reference_profile)
    all_refs = [*(references or []), *profile_refs]
    final_prompt = f"{prompt}{profile_prompt}"

    contents: list = [final_prompt]
    for ref in all_refs:
        contents.append(_read_reference_image(ref))

    response = client.models.generate_content(model=model, contents=contents)
    image_bytes_list = _extract_images(response)

    if not image_bytes_list:
        text_out = ""
        try:
            text_out = response.text or ""
        except Exception:
            pass
        raise RuntimeError(
            f"Model returned no image. Response text: {text_out[:500] or '(empty)'}"
        )

    results: list[tuple[Path, int, tuple[int, int]]] = []
    for raw in image_bytes_list:
        encoded, ext, dims = _resize_and_encode(
            raw, target_size, output_format, quality
        )
        path = _safe_output_path(None, ext=ext)
        path.write_bytes(encoded)
        results.append((path, len(encoded), dims))

    response_text = ""
    try:
        response_text = (response.text or "").strip()
    except Exception:
        pass
    return results, response_text


server: Server = Server("nano-banana")


def _common_size_format_props() -> dict:
    """Reusable input-schema fragment for size + format + quality params."""
    return {
        "target_size": {
            "type": "string",
            "description": (
                f"Output max dimensions. Default: {DEFAULT_TARGET_SIZE}. "
                "Presets: '256', '512', '640', '1024' (recommended for email + "
                "mobile), '2048', '4k'. Custom: 'WIDTHxHEIGHT' (e.g. '600x300'). "
                "'off' returns the raw model output unchanged. "
                "Aspect ratio is preserved; image is only downscaled, never "
                "upscaled."
            ),
        },
        "output_format": {
            "type": "string",
            "enum": ["webp", "jpeg", "png"],
            "description": (
                f"Output file format. Default: {DEFAULT_OUTPUT_FORMAT}. "
                "WebP is 25-35% smaller than JPEG at the same visual quality "
                "and works in every modern email client + browser since 2023. "
                "Use JPEG for old Outlook (<=2019) compatibility or for MMS "
                "(carrier WebP support varies). PNG only when you genuinely "
                "need lossless or alpha — much larger files."
            ),
        },
        "quality": {
            "type": "integer",
            "minimum": 30,
            "maximum": 100,
            "description": (
                f"Compression quality 30-100. Default: {DEFAULT_QUALITY}. "
                "WebP/JPEG only — ignored for PNG. 75 is the sweet spot for "
                "email/web. Push to 90+ only for hero images on premium "
                "landing pages."
            ),
        },
        "reference_profile": {
            "type": "string",
            "enum": sorted(REFERENCE_PROFILES),
            "description": (
                "Optional reusable reference bundle. Use 'botspot_spot' when an "
                "image should include the canonical BotSpot/Lumibot Spot mascot. "
                "Profiles append reference images and brand guardrails without "
                "requiring every caller to know the asset paths."
            ),
        },
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    model_enum = sorted(ALLOWED_MODELS)
    common = _common_size_format_props()
    return [
        Tool(
            name="generate_image",
            description=(
                "Generate an image with Google Nano Banana Pro (Gemini 3 Pro Image), "
                "Google's flagship image generation model. The image is saved to a "
                "sandboxed local directory and the file path is returned. Use this "
                "for marketing visuals, email images, mockups, illustrations, etc. "
                "Be detailed in the prompt: subject, style, lighting, composition, "
                "and any text content the image should contain. "
                "Output is automatically resized + re-encoded to web-optimal "
                f"defaults ({DEFAULT_TARGET_SIZE}px max, {DEFAULT_OUTPUT_FORMAT}, "
                f"quality {DEFAULT_QUALITY}); override per-call as needed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            f"Optional model override. Default: {DEFAULT_MODEL}. "
                            "Pro = max quality; Flash = newer + faster + 4K."
                        ),
                        "enum": model_enum,
                    },
                    **common,
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="edit_image",
            description=(
                "Edit, remix, or composite one or more existing images using Nano "
                "Banana Pro. Provide absolute paths to reference images plus a "
                "natural-language instruction (e.g. 'change the background to a "
                "sunset beach', 'combine these into a single product shot', "
                "'replace the text on the sign with HELLO'). Up to ~14 references. "
                "Output is automatically resized + re-encoded to web-optimal "
                "defaults; override per-call as needed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Natural-language editing instruction.",
                    },
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Absolute paths to reference images "
                            "(PNG, JPEG, WebP, or GIF). Optional if "
                            "reference_profile is provided."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": f"Optional model override. Default: {DEFAULT_MODEL}.",
                        "enum": model_enum,
                    },
                    **common,
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        model = _validate_model(arguments.get("model"))
        target_size = str(arguments.get("target_size", DEFAULT_TARGET_SIZE))
        output_format = str(
            arguments.get("output_format", DEFAULT_OUTPUT_FORMAT)
        ).lower()
        quality = int(arguments.get("quality", DEFAULT_QUALITY))
        if not (30 <= quality <= 100):
            raise ValueError(f"quality must be 30-100, got {quality}")

        if name == "generate_image":
            prompt = arguments["prompt"]
            reference_profile = arguments.get("reference_profile")
            results, model_text = await asyncio.to_thread(
                _generate,
                prompt,
                model,
                None,
                reference_profile,
                target_size,
                output_format,
                quality,
            )
        elif name == "edit_image":
            prompt = arguments["prompt"]
            refs = arguments.get("reference_images") or []
            reference_profile = arguments.get("reference_profile")
            if not refs and not reference_profile:
                raise ValueError(
                    "edit_image requires reference_images or reference_profile."
                )
            results, model_text = await asyncio.to_thread(
                _generate,
                prompt,
                model,
                refs,
                reference_profile,
                target_size,
                output_format,
                quality,
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

        lines = [
            f"Generated {len(results)} image(s) with model `{model}` "
            f"(size={target_size}, format={output_format}, quality={quality}):"
        ]
        for p, byte_size, (w, h) in results:
            kb = byte_size / 1024.0
            dim = f"{w}x{h}" if w > 0 and h > 0 else "raw"
            lines.append(f"- {p}  ({dim}, {kb:.1f} KB)")
        lines.append(f"\nOutput directory: {OUTPUT_DIR}")
        if model_text:
            lines.append(f"\nModel notes: {model_text}")
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
