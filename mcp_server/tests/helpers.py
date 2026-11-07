"""Shared helpers for MCP tool tests."""

from __future__ import annotations

import json
from typing import Any

from mcp import Client
from mcp.types import TextContent


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(name, arguments or {})
    if result.is_error:
        text = _text(result)
        raise AssertionError(f"{name} failed: {text}")
    if result.structured_content is not None:
        return result.structured_content
    text = _text(result)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _text(result: Any) -> str:
    parts = []
    for block in result.content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif getattr(block, "text", None):
            parts.append(block.text)
    return "\n".join(parts)
