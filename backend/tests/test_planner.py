"""Planner parsing and DeepSeek tool-output recovery tests."""

from config import Configuration
from models import SummaryState
from services.planner import PlanningService


class FakePlannerAgent:
    """Minimal planner stand-in for parsing-only tests."""


def _service() -> PlanningService:
    return PlanningService(FakePlannerAgent(), Configuration(enable_notes=False))


def test_extracts_tasks_json_after_unrelated_tool_json() -> None:
    text = """
    [TOOL_CALL:note:{"action":"create","task_id":1}]
    已同步笔记。
    {"tasks":[{"title":"协议对比","intent":"比较协议","query":"SSE WebSocket"}]}
    """

    tasks = _service()._extract_tasks(text)

    assert tasks == [
        {"title": "协议对比", "intent": "比较协议", "query": "SSE WebSocket"}
    ]


def test_recovers_deepseek_planner_tasks_from_note_tool_events() -> None:
    state = SummaryState(research_topic="比较 SSE 与 WebSocket")
    events = [
        {
            "agent": "研究规划专家",
            "tool": "note",
            "parsed_parameters": {
                "action": "create",
                "task_id": 2,
                "title": "任务 2: 场景适配",
                "content": "分析 AI 进度展示场景。",
            },
        },
        {
            "agent": "研究规划专家",
            "tool": "note",
            "parsed_parameters": {
                "action": "create",
                "task_id": 1,
                "title": "协议基础",
                "content": "比较通信方向与连接方式。",
            },
        },
    ]

    tasks = PlanningService.recover_tasks_from_tool_events(state, events)

    assert [task.id for task in tasks] == [1, 2]
    assert tasks[0].title == "协议基础"
    assert tasks[0].intent == "比较通信方向与连接方式。"
    assert tasks[1].query == "比较 SSE 与 WebSocket 场景适配"
