"""Bedrock Claude Haiku wrapper for worker LLM calls (boto3 Converse API)."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))
BEDROCK_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0"))

# Lazy singleton — created on first use so import succeeds without AWS config.
_bedrock_runtime: Any | None = None


def _get_bedrock_runtime() -> Any:
    """Return a module-level bedrock-runtime client, creating it on first call."""
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_runtime


def invoke_haiku(*, system: str, user: str) -> str:
    """
    Invoke Claude Haiku on AWS Bedrock (live Converse API call) and return
    the assistant text.

    Raises:
        RuntimeError: On AWS/client failures (ClientError, BotoCoreError) or an
            unexpected response shape. Never returns None or an empty string on
            failure — callers (workers.py) catch this and fall back to heuristics.
    """
    client = _get_bedrock_runtime()
    try:
        response = client.converse(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={
                "maxTokens": BEDROCK_MAX_TOKENS,
                "temperature": BEDROCK_TEMPERATURE,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error(
            "Bedrock converse failed (model_id=%s region=%s): %s",
            BEDROCK_MODEL_ID,
            AWS_REGION,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Bedrock converse failed (model_id={BEDROCK_MODEL_ID}, "
            f"region={AWS_REGION}): {exc}"
        ) from exc

    try:
        content = response["output"]["message"]["content"]
        text = content[0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error(
            "Bedrock converse returned unexpected shape (model_id=%s region=%s): %s",
            BEDROCK_MODEL_ID,
            AWS_REGION,
            response,
            exc_info=True,
        )
        raise RuntimeError(
            f"Bedrock converse returned unexpected response shape "
            f"(model_id={BEDROCK_MODEL_ID}, region={AWS_REGION}): {exc}"
        ) from exc

    if not isinstance(text, str) or not text.strip():
        logger.error(
            "Bedrock converse returned empty text (model_id=%s region=%s)",
            BEDROCK_MODEL_ID,
            AWS_REGION,
        )
        raise RuntimeError(
            f"Bedrock converse returned empty text "
            f"(model_id={BEDROCK_MODEL_ID}, region={AWS_REGION})"
        )

    return text


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from model text (raw or fenced)."""
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"No JSON object found in model response: {text[:200]!r}")


def invoke_haiku_json(*, system: str, user: str) -> dict[str, Any]:
    """Invoke Haiku and parse a JSON object from the response."""
    return parse_json_object(invoke_haiku(system=system, user=user))


def get_bedrock_config_summary() -> dict[str, Any]:
    return {
        "model_id": BEDROCK_MODEL_ID,
        "region": AWS_REGION,
        "max_tokens": BEDROCK_MAX_TOKENS,
        "temperature": BEDROCK_TEMPERATURE,
        "wired": True,
    }
