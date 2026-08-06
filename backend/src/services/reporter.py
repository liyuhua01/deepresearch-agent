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
        report_text = self._clean_report(response)

        if not self._config.enable_source_provenance:
            self._agent.clear_history()
            return report_text or "报告生成失败，请检查输入。"

        report_text = expand_source_tokens(report_text, all_sources)
        audit = audit_provenance(
            report_text,
            sources=all_sources,
            claims=all_claims,
        )
        retry_attempted = self._needs_quality_retry(audit)
        retry_applied = False
        if retry_attempted:
            try:
                revised = self._clean_report(
                    self._agent.run(self._build_quality_revision_prompt(audit))
                )
                revised = expand_source_tokens(revised, all_sources)
                revised_audit = audit_provenance(
                    revised,
                    sources=all_sources,
                    claims=all_claims,
                )
                if self._is_better_revision(audit, revised_audit, revised):
                    report_text = revised
                    audit = revised_audit
                    retry_applied = True
            except Exception:
                # Quality rewriting is fail-open: retain the valid first report.
                retry_applied = False
        self._agent.clear_history()

        state.provenance_audit = asdict(audit)
        state.provenance_audit["report_quality_retry_attempted"] = retry_attempted
        state.provenance_audit["report_quality_retry_applied"] = retry_applied

        return report_text or "报告生成失败，请检查输入。"

    def _clean_report(self, text: str) -> str:
        cleaned = text.strip()
        if self._config.strip_thinking_tokens:
            cleaned = strip_thinking_tokens(cleaned)
        return strip_tool_calls(
            cleaned,
            include_dsml=self._config.enable_source_provenance,
        ).strip()

    @staticmethod
    def _needs_quality_retry(audit) -> bool:
        return bool(
            audit.report_citation_count_raw >= 6
            and (
                (audit.report_duplicate_citation_rate or 0) > 0.5
                or (audit.report_max_source_citation_share or 0) > 0.35
            )
        )

    @staticmethod
    def _build_quality_revision_prompt(audit) -> str:
        duplicate_rate = (audit.report_duplicate_citation_rate or 0) * 100
        max_share = (audit.report_max_source_citation_share or 0) * 100
        return (
            "请重写上一版完整报告，只优化引用质量，不调用任何工具，不输出 JSON/DSML。"
            "保留报告的核心事实、风险、建议和章节结构。\n"
            f"上一版有 {audit.report_citation_count_raw} 次引用、"
            f"{audit.report_unique_url_count} 个唯一 URL，重复率 {duplicate_rate:.1f}%，"
            f"最大单一来源占比 {max_share:.1f}%。\n"
            "要求：每个 URL 在全文最多出现 3 次；同一段优先保留 1-2 个最直接来源；"
            "参考来源章节每个 URL 只列一次；没有可靠支持的结论标注‘待验证’，不得"
            "为了提高覆盖率机械重复链接。只能使用上一版已有的来源编号或 URL。"
        )

    @staticmethod
    def _is_better_revision(original, revised, revised_text: str) -> bool:
        if not revised_text.strip():
            return False
        if revised.uncatalogued_url_count or revised.report_unknown_source_id_count:
            return False
        minimum_unique_sources = max(
            1,
            (original.report_unique_url_count * 7 + 9) // 10,
        )
        if revised.report_unique_url_count < minimum_unique_sources:
            return False
        original_coverage = original.final_claim_citation_coverage
        revised_coverage = revised.final_claim_citation_coverage
        if (
            original_coverage is not None
            and revised_coverage is not None
            and revised_coverage < original_coverage - 0.1
        ):
            return False
        original_duplicate = original.report_duplicate_citation_rate or 0
        revised_duplicate = revised.report_duplicate_citation_rate or 0
        original_share = original.report_max_source_citation_share or 0
        revised_share = revised.report_max_source_citation_share or 0
        return bool(
            (
                revised_duplicate <= original_duplicate - 0.1
                or revised_share <= original_share - 0.1
            )
        )
