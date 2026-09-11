"""Service that consolidates task results into the final report."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from hello_agents import ToolAwareSimpleAgent

from config import Configuration
from evaluation.provenance import (
    audit_provenance,
    collapse_adjacent_duplicate_citations,
    format_source_catalog,
    render_numbered_citations,
)
from models import SummaryState
from services.text_processing import strip_tool_calls
from utils import strip_thinking_tokens


class ReportingService:
    """Generates the final structured report."""

    def __init__(
        self, report_agent: ToolAwareSimpleAgent, config: Configuration
    ) -> None:
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

        notes_section = (
            "\n".join(note_references) if note_references else "- 暂无可用任务笔记"
        )

        all_sources = [
            source for task in state.todo_items for source in task.source_records
        ]
        all_claims = [
            claim for task in state.todo_items for claim in task.claim_mappings
        ]
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
                "- 以核心章节、关键主题段落或重要结论为单位附来源编号；同一段中的"
                "多句话可以共用段尾引用，无需每句话都添加。\n"
                "- 请输出 `[T1-S1]`，系统会将它转换为可点击的数字引用；"
                "也可直接输出 `[来源标题](URL)`。\n"
                "- `T1-C1` 是内部结论编号，不是来源编号，禁止把"
                " `[T1-C1]` 当成最终引用。\n"
                "- 只能使用目录中存在的编号和 URL。直接支撑核心结论的来源优先放在"
                "相关段落末尾；仅作为背景阅读的资料可以只列在参考来源章节。\n"
                "- 标有‘相关性待复核’的来源不得单独支撑核心结论；无法确认相关性时"
                "应忽略或标注待验证。\n"
                "- 参考来源章节仍需保留，并按任务汇总。\n"
                "</最终报告引用要求>\n"
            )

        read_template = json.dumps(
            {"action": "read", "note_id": "<note_id>"}, ensure_ascii=False
        )
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
        generation_retry_attempted = False
        generation_retry_applied = False
        generation_fallback_applied = False
        if not self._is_substantive_report(report_text):
            generation_retry_attempted = True
            try:
                recovered = self._clean_report(
                    self._agent.run(self._build_report_only_prompt(state, all_sources))
                )
                if self._is_substantive_report(recovered):
                    report_text = recovered
                    generation_retry_applied = True
            except Exception:
                generation_retry_applied = False
        if not self._is_substantive_report(report_text):
            report_text = self._build_summary_fallback(state)
            generation_fallback_applied = True

        if not self._config.enable_source_provenance:
            self._agent.clear_history()
            return report_text

        report_text = collapse_adjacent_duplicate_citations(
            render_numbered_citations(
                report_text,
                sources=all_sources,
                claims=all_claims,
            )
        )
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
                revised = collapse_adjacent_duplicate_citations(
                    render_numbered_citations(
                        revised,
                        sources=all_sources,
                        claims=all_claims,
                    )
                )
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
        state.provenance_audit["report_generation_retry_attempted"] = (
            generation_retry_attempted
        )
        state.provenance_audit["report_generation_retry_applied"] = (
            generation_retry_applied
        )
        state.provenance_audit["report_generation_fallback_applied"] = (
            generation_fallback_applied
        )

        return report_text

    def _clean_report(self, text: str) -> str:
        cleaned = text.strip()
        if self._config.strip_thinking_tokens:
            cleaned = strip_thinking_tokens(cleaned)
        return strip_tool_calls(
            cleaned,
            include_dsml=self._config.enable_source_provenance,
        ).strip()

    @staticmethod
    def _is_substantive_report(text: str) -> bool:
        """Reject empty fences/tool residue while allowing concise valid reports."""
        meaningful = re.findall(r"[A-Za-z0-9\u3400-\u9fff]", text)
        return len(meaningful) >= 8

    @staticmethod
    def _build_report_only_prompt(state: SummaryState, sources: list) -> str:
        source_lines = "\n".join(
            f"- [{source.source_id}]({source.normalized_url}) {source.title}"
            for source in sources
        )
        return (
            "笔记读取已经完成。上一条回复没有形成有效报告。现在禁止调用任何工具，"
            "不要输出 JSON、DSML、代码围栏或过程说明；只输出完整 Markdown 研究报告。"
            "报告至少包含：执行摘要、分主题发现、证据不足与风险、结论、参考来源。"
            "只能使用下列来源编号及其对应 URL；证据不足的事实必须标注‘待验证’。\n"
            f"研究主题：{state.research_topic}\n来源目录：\n{source_lines or '- 暂无来源'}"
        )

    @staticmethod
    def _build_summary_fallback(state: SummaryState) -> str:
        """Produce a useful deterministic report when the model emits no report."""
        sections = [
            f"# {state.research_topic}",
            "",
            "## 执行摘要",
            "最终整合步骤未返回有效正文。以下内容由已完成的分项研究结果自动整理；"
            "未被现有来源直接支持的结论均应视为待验证。",
        ]
        for task in state.todo_items:
            sections.extend(
                [
                    "",
                    f"## {task.title}",
                    task.summary or "暂无可用信息。",
                    "",
                    "### 本项来源",
                    task.sources_summary or "暂无来源。",
                ]
            )
        sections.extend(
            [
                "",
                "## 结论与限制",
                "本报告保留各分项研究的原始证据边界。对于缺少权威来源、时间信息"
                "或交叉验证的内容，应在采取行动前继续核实。",
            ]
        )
        return "\n".join(sections).strip()

    @staticmethod
    def _needs_quality_retry(audit) -> bool:
        return bool(
            audit.report_citation_count_raw >= 6
            and (audit.report_duplicate_citation_rate or 0) > 0.5
            and (audit.report_max_source_citation_share or 0) > 0.35
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
        original_integrity = original.report_relevant_source_integrity_rate
        revised_integrity = revised.report_relevant_source_integrity_rate
        if (
            original_integrity is not None
            and revised_integrity is not None
            and revised_integrity < original_integrity
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
