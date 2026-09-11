"""Contract tests for provider, estimated and unavailable LLM usage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException

from evaluation.instrumented_llm import InstrumentedLLM
from evaluation.telemetry import RunRecorder


class FakeCompletions:
    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _client(response):
    completions = FakeCompletions(response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _llm(monkeypatch, response, *, fallback="unavailable", stream_mode="auto"):
    recorder = RunRecorder(run_id="run_llm_test", topic="LLM 测试")
    client, completions = _client(response)
    monkeypatch.setattr(HelloAgentsLLM, "_create_client", lambda _self: client)
    llm = InstrumentedLLM(
        model="deepseek-chat",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        provider="custom",
        recorder=recorder,
        token_usage_fallback=fallback,
        stream_usage_mode=stream_mode,
    )
    return llm, recorder, completions


def test_non_streaming_output_is_unchanged_and_provider_usage_is_recorded(
    monkeypatch,
) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="原样输出"))],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        ),
    )
    llm, recorder, _ = _llm(monkeypatch, response)

    assert llm.invoke([{"role": "user", "content": "测试"}]) == "原样输出"
    metrics = recorder.snapshot()
    assert metrics["llm_calls"] == 1
    assert metrics["prompt_tokens"] == 11
    assert metrics["completion_tokens"] == 7
    assert metrics["total_tokens"] == 18
    assert metrics["usage_source"] == "provider"


def test_streaming_output_chunks_are_unchanged_and_final_usage_is_recorded(
    monkeypatch,
) -> None:
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="甲"))], usage=None
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="乙"))], usage=None
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=9,
                completion_tokens=2,
                total_tokens=11,
            ),
        ),
    ]
    llm, recorder, completions = _llm(monkeypatch, iter(chunks))

    assert list(llm.stream_invoke([{"role": "user", "content": "测试"}])) == [
        "甲",
        "乙",
    ]
    assert completions.requests[0]["stream_options"] == {"include_usage": True}
    metrics = recorder.snapshot()
    assert metrics["total_tokens"] == 11
    assert metrics["usage_source"] == "provider"


def test_estimated_fallback_is_explicit(monkeypatch) -> None:
    class FakeEncoding:
        @staticmethod
        def encode(text: str) -> list[str]:
            return list(text)

    monkeypatch.setattr(
        "tiktoken.encoding_for_model", lambda _model: FakeEncoding()
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="估算输出"))],
        usage=None,
    )
    llm, recorder, _ = _llm(monkeypatch, response, fallback="estimated")

    assert llm.invoke([{"role": "user", "content": "估算输入"}]) == "估算输出"
    metrics = recorder.snapshot()
    assert metrics["total_tokens"] > 0
    assert metrics["usage_source"] == "estimated"


def test_unavailable_fallback_does_not_invent_tokens(monkeypatch) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="无统计输出"))],
        usage=None,
    )
    llm, recorder, _ = _llm(monkeypatch, response)

    assert llm.invoke([{"role": "user", "content": "输入"}]) == "无统计输出"
    metrics = recorder.snapshot()
    assert metrics["total_tokens"] is None
    assert metrics["usage_source"] == "unavailable"


def test_llm_failure_preserves_exception_contract_and_counts_failure(monkeypatch) -> None:
    llm, recorder, _ = _llm(monkeypatch, TimeoutError("provider timeout"))

    with pytest.raises(HelloAgentsException, match="LLM调用失败"):
        llm.invoke([{"role": "user", "content": "输入"}])

    metrics = recorder.snapshot()
    assert metrics["llm_calls"] == 1
    assert metrics["llm_failures"] == 1
