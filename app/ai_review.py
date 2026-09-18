import os
import json
import base64
import logging
from functools import lru_cache
from io import BytesIO
from PIL import Image
from openai import OpenAI
from .config import get_openai_config

logger = logging.getLogger("uvicorn.error")


@lru_cache(maxsize=1)
def _get_client():
    cfg = get_openai_config()
    return OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60, max_retries=0)


def _empty_review(reason: str) -> dict:
    return {
        "is_target": False,
        "confidence": 0.0,
        "matched_required": [],
        "missed_required": [],
        "matched_bonus": [],
        "missed_bonus": [],
        "reason": reason,
    }


def _as_confidence(value) -> float:
    """Coerce an AI-returned confidence into a 0.0-1.0 float, defaulting safely."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _as_list(value) -> list:
    """Coerce an AI-returned field into a list of strings. Bare strings are rejected."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _image_to_base64_jpeg(image_path: str) -> str:
    """Open an image (any format Pillow supports) and return JPEG bytes as base64.
    Downscales to at most 1200px so we never ship a full-res phone photo to the API.
    This ensures OpenAI-compatible endpoints always receive a JPEG."""
    with Image.open(image_path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.thumbnail((1200, 1200), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def review_image(
    image_path: str,
    item_name: str,
    item_description: str,
    required_properties: list,
    bonus_properties: list,
) -> dict:
    """Send the image to the OpenAI-compatible endpoint and parse the structured response."""
    cfg = get_openai_config()

    if not cfg["api_key"]:
        logger.error("OPENAI_API_KEY env var is not set — cannot run AI review.")
        return _empty_review("AI service not configured (missing API key).")

    try:
        b64 = _image_to_base64_jpeg(image_path)
    except Exception as e:
        logger.error(f"Failed to encode image for AI review: {e}")
        return _empty_review("Failed to process image.")

    required_section = "\n".join(f"- {p}" for p in required_properties)
    if bonus_properties:
        bonus_section = (
            "\n\nBONUS properties (match ALL of these to earn extra points):\n"
            + "\n".join(f"- {p}" for p in bonus_properties)
        )
    else:
        bonus_section = ""

    prompt = (
        f"You are judging a scavenger hunt photo. The target item is: '{item_name}'.\n"
        f"Item description: {item_description}\n\n"
        f"REQUIRED properties (the photo must match ALL of these to be approved):\n"
        f"{required_section}"
        f"{bonus_section}\n\n"
        f"Respond ONLY with this exact JSON structure (no extra text):\n"
        "{\n"
        '  "is_target": true/false,\n'
        '  "confidence": 0.0-1.0,\n'
        '  "matched_required": ["list of property strings that match"],\n'
        '  "missed_required": ["list of property strings that do NOT match"],\n'
        '  "matched_bonus": ["list of bonus property strings that match"],\n'
        '  "missed_bonus": ["list of bonus property strings that do NOT match"],\n'
        '  "reason": "one short sentence explaining the decision"\n'
        "}"
    )

    try:
        response = _get_client().chat.completions.create(
            model=cfg["model"],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }],
            max_tokens=500,
        )
        raw = response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI API call failed: {e}")
        return _empty_review("AI service unavailable.")

    if not raw or not isinstance(raw, str):
        logger.error(f"AI returned an empty response: {raw!r}")
        return _empty_review("AI returned an empty response.")

    try:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            parsed = json.loads(raw[start:end + 1])

        if not isinstance(parsed, dict):
            raise ValueError("AI response was not a JSON object")

        return {
            "is_target": bool(parsed.get("is_target", False)),
            "confidence": _as_confidence(parsed.get("confidence", 0.0)),
            "matched_required": _as_list(parsed.get("matched_required")),
            "missed_required": _as_list(parsed.get("missed_required")),
            "matched_bonus": _as_list(parsed.get("matched_bonus")),
            "missed_bonus": _as_list(parsed.get("missed_bonus")),
            "reason": str(parsed.get("reason", "No reason provided.")),
        }
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"Could not parse AI review response ({e}): {raw}")
        return _empty_review("AI returned an invalid response.")