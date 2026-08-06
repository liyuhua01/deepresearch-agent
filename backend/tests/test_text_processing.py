"""Tests for removing model-emitted tool-call envelopes."""

from services.text_processing import strip_tool_calls


def test_strip_dsml_tool_block_keeps_following_user_summary() -> None:
    text = """<｜｜DSML｜｜TOOL_CALL_OVERALL>
```json
[{"name":"note","arguments":{"content":"internal"}}]
```
</｜｜DSML｜｜TOOL_CALL_OVERALL>
## 任务总结
- asyncio 适合网络 I/O。
"""

    assert strip_tool_calls(text, include_dsml=True).strip() == (
        "## 任务总结\n- asyncio 适合网络 I/O。"
    )


def test_strip_dsml_tool_only_response_becomes_empty() -> None:
    text = (
        "<||DSML||TOOL_CALL_OVERALL>tool payload"
        "</||DSML||TOOL_CALL_OVERALL>"
    )

    assert strip_tool_calls(text, include_dsml=True) == ""


def test_dsml_cleaning_is_opt_in_for_backward_compatibility() -> None:
    text = (
        "<||DSML||TOOL_CALL_OVERALL>tool payload"
        "</||DSML||TOOL_CALL_OVERALL>"
    )

    assert strip_tool_calls(text) == text
