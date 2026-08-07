"""Task summarization utilities."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Tuple

from hello_agents import ToolAwareSimpleAgent

from config import Configuration
from models import SummaryState, TodoItem
from services.notes import build_note_guidance
from services.text_processing import strip_tool_calls
from utils import strip_thinking_tokens


class SummarizationService:
    """Handles synchronous and streaming task summarization."""

    def __init__(
        self,
        summarizer_factory: Callable[[], ToolAwareSimpleAgent],
        config: Configuration,
    ) -> None:
        self._agent_factory = summarizer_factory
        self._config = config

    def summarize_task(self, state: SummaryState, task: TodoItem, context: str) -> str:
        """Generate a task-specific summary using the summarizer agent."""

        prompt = self._build_prompt(state, task, context)

        agent = self._agent_factory()
        try:
            response = agent.run(prompt)
            summary_text = self._clean_summary(response)
            if (
                self._config.enable_source_provenance
                and not self._is_substantive_summary(summary_text)
            ):
                recovered = self._clean_summary(
                    agent.run(self._build_summary_only_prompt(task))
                )
                if recovered:
                    summary_text = recovered
        finally:
            agent.clear_history()

        return summary_text or "暂无可用信息"

    def stream_task_summary(
        self, state: SummaryState, task: TodoItem, context: str
    ) -> Tuple[Iterator[str], Callable[[], str]]:
        """Stream the summary text for a task while collecting full output."""

        prompt = self._build_prompt(state, task, context)
        remove_thinking = self._config.strip_thinking_tokens
        raw_buffer = ""
        visible_output = ""
        emit_index = 0
        agent = self._agent_factory()

        def flush_visible() -> Iterator[str]:
            nonlocal emit_index, raw_buffer
            while True:
                start = raw_buffer.find("<think>", emit_index)
                if start == -1:
                    if emit_index < len(raw_buffer):
                        segment = raw_buffer[emit_index:]
                        emit_index = len(raw_buffer)
                        if segment:
                            yield segment
                    break

                if start > emit_index:
                    segment = raw_buffer[emit_index:start]
                    emit_index = start
                    if segment:
                        yield segment

                end = raw_buffer.find("</think>", start)
                if end == -1:
                    break
                emit_index = end + len("</think>")

        def generator() -> Iterator[str]:
            nonlocal raw_buffer, visible_output, emit_index
            try:
                if self._config.enable_source_provenance:
                    for chunk in agent.stream_run(prompt):
                        raw_buffer += chunk
                    visible_output = self._clean_summary(raw_buffer)
                    if not self._is_substantive_summary(visible_output):
                        recovered = self._clean_summary(
                            agent.run(self._build_summary_only_prompt(task))
                        )
                        if recovered:
                            visible_output = recovered
                    if visible_output:
                        yield visible_output
                    return

                for chunk in agent.stream_run(prompt):
                    raw_buffer += chunk
                    if remove_thinking:
                        for segment in flush_visible():
                            visible_output += segment
                            if segment:
                                yield segment
                    else:
                        visible_output += chunk
                        if chunk:
                            yield chunk
            finally:
                if remove_thinking:
                    for segment in flush_visible():
                        visible_output += segment
                        if segment:
                            yield segment
                agent.clear_history()

        def get_summary() -> str:
            if self._config.enable_source_provenance:
                return visible_output.strip()
            if remove_thinking:
                cleaned = strip_thinking_tokens(visible_output)
            else:
                cleaned = visible_output

            return strip_tool_calls(cleaned).strip()

        return generator(), get_summary

    def _clean_summary(self, text: str) -> str:
        """Remove reasoning and tool envelopes from user-visible summary text."""
        cleaned = text.strip()
        if self._config.strip_thinking_tokens:
            cleaned = strip_thinking_tokens(cleaned)
        cleaned = strip_tool_calls(
            cleaned,
            include_dsml=self._config.enable_source_provenance,
        ).strip()
        if self._config.enable_source_provenance:
            heading = re.search(r"(?m)^#{1,6}\s*任务总结\s*$", cleaned)
            if heading:
                cleaned = cleaned[heading.start() :].strip()
        return cleaned

    @staticmethod
    def _is_substantive_summary(text: str) -> bool:
        """Require the explicit user-summary contract, not tool payload prose."""
        return bool(re.search(r"(?m)^#{1,6}\s*任务总结\s*$", text))

    @staticmethod
    def _build_summary_only_prompt(task: TodoItem) -> str:
        """Request the missing user-facing answer after a tool-only model turn."""
        source_lines = "\n".join(
            f"- [{source.source_id}]({source.normalized_url}) {source.title}"
            for source in task.source_records
        )
        return (
            "笔记工具调用已经完成。现在禁止再次调用任何工具，也不要输出 JSON、"
            "DSML 或工具调用包装。请只输出面向用户的 Markdown 任务总结，包含 3-5 条"
            "有实质内容的关键发现。按主题组织段落或要点组，每个关键主题至少附一个"
            "目录中的完整引用链接；同一段中的多句话可以共用段尾来源，无需逐句添加。"
            "来源编号与 URL 必须严格配对；证据不足时标注‘待验证’。\n"
            f"来源目录：\n{source_lines}"
        )

    def _build_prompt(self, state: SummaryState, task: TodoItem, context: str) -> str:
        """Construct the summarization prompt shared by both modes."""

        provenance_guidance = ""
        if self._config.enable_source_provenance and task.source_records:
            allowed_ids = ", ".join(source.source_id for source in task.source_records)
            provenance_guidance = (
                "\n<来源溯源要求>\n"
                f"- 本任务只允许使用这些来源编号：{allowed_ids}。\n"
                "- 按主题组织关键段落或要点组，每个关键主题至少附一个来源；同一段内的"
                "多条事实、数字或比较判断可以共用段尾来源，无需逐句重复。格式必须为"
                " `[来源编号](对应URL)`。\n"
                "- 不得编造来源编号或 URL；没有证据的判断必须明确标注‘待验证’。\n"
                "- 标有‘相关性待复核’的来源不得单独支撑核心结论；只有证据正文"
                "明确支持当前任务时才能引用，否则忽略。\n"
                "</来源溯源要求>\n"
            )

        return (
            f"任务主题：{state.research_topic}\n"
            f"任务名称：{task.title}\n"
            f"任务目标：{task.intent}\n"
            f"检索查询：{task.query}\n"
            f"任务上下文：\n{context}\n"
            f"{provenance_guidance}"
            f"{build_note_guidance(task)}\n"
            "请按照以上协作要求先同步笔记，然后返回一份面向用户的 Markdown 总结（仍遵循任务总结模板）。"
        )
