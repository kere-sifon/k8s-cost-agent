"""Tests for Bedrock Converse wiring (mocked — no live AWS calls)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import mcp_server.agent.bedrock_client as bedrock_client
from mcp_server.agent.bedrock_client import (
    get_bedrock_config_summary,
    invoke_haiku,
    invoke_haiku_json,
)


@pytest.fixture(autouse=True)
def _reset_bedrock_singleton():
    """Ensure lazy client is recreated per test."""
    bedrock_client._bedrock_runtime = None
    yield
    bedrock_client._bedrock_runtime = None


def test_get_bedrock_config_summary_wired() -> None:
    summary = get_bedrock_config_summary()
    assert summary["wired"] is True
    assert "model_id" in summary
    assert "region" in summary


def test_invoke_haiku_converse_success() -> None:
    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "output": {"message": {"content": [{"text": '{"candidates": []}'}]}}
    }

    with patch.object(bedrock_client, "_get_bedrock_runtime", return_value=mock_client):
        text = invoke_haiku(system="sys", user="usr")

    assert text == '{"candidates": []}'
    mock_client.converse.assert_called_once()
    kwargs = mock_client.converse.call_args.kwargs
    assert kwargs["modelId"] == bedrock_client.BEDROCK_MODEL_ID
    assert kwargs["system"] == [{"text": "sys"}]
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "usr"}]}]
    assert kwargs["inferenceConfig"]["maxTokens"] == bedrock_client.BEDROCK_MAX_TOKENS
    assert kwargs["inferenceConfig"]["temperature"] == bedrock_client.BEDROCK_TEMPERATURE


def test_invoke_haiku_json_parses_response() -> None:
    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": '{"candidates": [{"id": "a"}]}'}]
            }
        }
    }

    with patch.object(bedrock_client, "_get_bedrock_runtime", return_value=mock_client):
        result = invoke_haiku_json(system="sys", user="usr")

    assert result == {"candidates": [{"id": "a"}]}


def test_invoke_haiku_client_error_raises_runtime_error() -> None:
    mock_client = MagicMock()
    mock_client.converse.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "Converse",
    )

    with patch.object(bedrock_client, "_get_bedrock_runtime", return_value=mock_client):
        with pytest.raises(RuntimeError, match="Bedrock converse failed") as excinfo:
            invoke_haiku(system="sys", user="usr")

    assert bedrock_client.BEDROCK_MODEL_ID in str(excinfo.value)
    assert bedrock_client.AWS_REGION in str(excinfo.value)


def test_invoke_haiku_bad_shape_raises_runtime_error() -> None:
    mock_client = MagicMock()
    mock_client.converse.return_value = {"output": {"message": {"content": []}}}

    with patch.object(bedrock_client, "_get_bedrock_runtime", return_value=mock_client):
        with pytest.raises(RuntimeError, match="unexpected response shape"):
            invoke_haiku(system="sys", user="usr")


def test_lazy_client_not_created_at_import() -> None:
    """Module import must not require AWS; singleton starts as None."""
    assert bedrock_client._bedrock_runtime is None
