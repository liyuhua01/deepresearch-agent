"""Utility helpers for normalizing agent generated text."""

from __future__ import annotations

import re

_DSML_TOOL_BLOCK = re.compile(
    r"<(?:｜｜|\|\|)DSML(?:｜｜|\|\|)TOOL_CALL_OVERALL>.*?"
    r"</(?:｜｜|\|\|)DSML(?:｜｜|\|\|)TOOL_CALL_OVERALL>",
    flags=re.DOTALL | re.IGNORECASE,
)


def strip_tool_calls(text: str, *, include_dsml: bool = False) -> str:
    """移除文本中的工具调用标记。"""

    if not text:
        return text

    if include_dsml:
        text = _DSML_TOOL_BLOCK.sub("", text)
    pattern = re.compile(r"\[TOOL_CALL:[^\]]+\]")
    return pattern.sub("", text)
