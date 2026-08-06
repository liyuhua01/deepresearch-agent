"""Service that consolidates task results into the final report."""

from __future__ import annotations

import json
from dataclasses import asdict

from hello_agents import ToolAwareSimpleAgent

from config import Configuration
from evaluation.provenance import (
    audit_provenance,
    expand_source_tokens,
    format_source_catalog,
)
from models import SummaryState
from services.text_processing import strip_tool_calls
from utils import strip_thinking_tokens


class ReportingService:
    """Generates the final structured report."""

    def __init__(self, report_agent: ToolAwareSimpleAgent, config: Configuration) -> None:
        self._agent = report_agent
        self._config = config

    def generate_report(self, state: SummaryState) -> str:
        """Generate a structured report based on completed tasks."""

        tasks_block = []
        for task in state.todo_items:
            summary_block = task.summary or "暂无可用信息"
            sources_block = task.sources_summary or "暂无来源"
            tasks_block.append(
                f"### 任务 {task.id}: {task.title}\n"
                f"- 任务目标：{task.intent}\n"
                f"- 检索查询：{task.query}\n"
                f"- 执行状态：{task.status}\n"
                f"- 任务总结：\n{summary_block}\n"
                f"- 来源概览：\n{sources_block}\n"
            )

        note_references = []
        for task in state.todo_items:
            if task.note_id:
                note_references.append(
                    f"- 任务 {task.id}《{task.title}》：note_id={task.note_id}"
                )

        notes_section = "\n".join(note_references) if note_references else "- 暂无可用任务笔记"

        all_sources = [source for task in state.todo_items for source in task.source_records]
        all_claims = [claim for task in state.todo_items for claim in task.claim_mappings]
        provenance_section = ""
        if self._config.enable_source_provenance and all_sources:
            claim_lines = []
            for claim in all_claims:
                source_ids = ", ".join(claim.source_ids) or "无已验证来源"
                claim_lines.append(
                    f"- {claim.claim_id}: {claim.text}\n  来源ID: {source_ids}"
                )
            provenance_section = (
                "\n来源编号目录：\n"
                f"{format_source_catalog(all_sources)}\n"
                "\n已提取的结论—来源映射：\n"
                f"{chr(10).join(claim_lines) or '- 暂无映射'}\n"
                "\n<最终报告引用要求>\n"
                "- 每条事实性结论后紧邻一个或多个来源编号。\n"
                "- 可输出 `[T1-S1]`，系统会展开为来源标题和真实链接；"
                "也可直接输出 `[来源标题](URL)`。\n"
                "- 只能使用目录中存在的编号和 URL，不得把引用集中到文末代替内联引用。\n"
                "- 标有‘相关性待复核’的来源不得单独支撑核心结论；无法确认相关性时"
                "应忽略或标注待验证。\n"
                "- 参考来源章节仍需保留，并按任务汇总。\n"
                "</最终报告引用要求>\n"
            )

        read_template = json.dumps({"action": "read", "note_id": "<note_id>"}, ensure_ascii=False)
        create_conclusion_template = json.dumps(
            {
                "action": "create",
                "title": f"研究报告：{state.research_topic}",
                "note_type": "conclusion",
                "tags": ["deep_research", "report"],
                "content": "请在此沉淀最终报告要点",
            },
            ensure_ascii=False,
        )

        prompt = (
            f"研究主题：{state.research_topic}\n"
            f"任务概览：\n{''.join(tasks_block)}\n"
            f"可用任务笔记：\n{notes_section}\n"
            f"{provenance_section}"
            f"请针对每条任务笔记使用格式：[TOOL_CALL:note:{read_template}] 读取内容，整合所有信息后撰写报告。\n"
            f"如需输出汇总结论，可追加调用：[TOOL_CALL:note:{create_conclusion_template}] 保存报告要点。"
        )

        response = self._agent.run(prompt)
        self._agent.clear_history()

        report_text = response.strip()
        if self._config.strip_thinking_tokens:
            report_text = strip_thinking_tokens(report_text)

        report_text = strip_tool_calls(report_text).strip()

        if self._config.enable_source_provenance:
            report_text = expand_source_tokens(report_text, all_sources)
            state.provenance_audit = asdict(
                audit_provenance(
                    report_text,
                    sources=all_sources,
                    claims=all_claims,
                )
            )

        return report_text or "报告生成失败，请检查输入。"
