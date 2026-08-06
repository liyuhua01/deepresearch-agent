"""HelloAgents-compatible LLM wrapper with fail-open usage telemetry."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException

from evaluation.telemetry import RunRecorder


class InstrumentedLLM(HelloAgentsLLM):
    """Preserve model output while recording calls and token usage."""

    def __init__(
        self,
        *args: Any,
        recorder: RunRecorder,
        token_usage_fallback: str = "unavailable",
        stream_usage_mode: str = "auto",
        **kwargs: Any,
    ) -> None:
        """Initialize the compatible client and telemetry policy."""
        self._recorder = recorder
        self._token_usage_fallback = token_usage_fallback
        self._stream_usage_mode = stream_usage_mode
        super().__init__(*args, **kwargs)

    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Return the exact non-streaming text and observe response usage."""
        self._recorder.record_llm_call()
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
                **{
                    key: value
                    for key, value in kwargs.items()
                    if key not in {"temperature", "max_tokens"}
                },
            )
            content = response.choices[0].message.content
            if not self._record_provider_usage(getattr(response, "usage", None)):
                self._record_fallback_usage(messages, content or "")
            return content
        except Exception as exc:
            self._recorder.record_llm_failure()
            raise HelloAgentsException(f"LLM调用失败: {str(exc)}") from exc

    def stream_invoke(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[str]:
        """Yield identical text chunks and capture the final usage chunk when supported."""
        yield from self._stream(messages, temperature=kwargs.get("temperature"))

    def think(
        self, messages: list[dict[str, str]], temperature: float | None = None
    ) -> Iterator[str]:
        """Keep the HelloAgents streaming entrypoint compatible."""
        yield from self._stream(messages, temperature=temperature)

    def _stream(
        self, messages: list[dict[str, str]], *, temperature: float | None
    ) -> Iterator[str]:
        self._recorder.record_llm_call()
        output_parts: list[str] = []
        usage_recorded = False
        stream_completed = False
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature if temperature is not None else self.temperature,
                "max_tokens": self.max_tokens,
                "stream": True,
            }
            if self._include_stream_usage():
                request["stream_options"] = {"include_usage": True}
            response = self._client.chat.completions.create(**request)

            for chunk in response:
                if self._record_provider_usage(getattr(chunk, "usage", None)):
                    usage_recorded = True
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                content = getattr(choices[0].delta, "content", None) or ""
                if content:
                    output_parts.append(content)
                    yield content
            stream_completed = True
        except GeneratorExit:
            raise
        except Exception as exc:
            self._recorder.record_llm_failure()
            raise HelloAgentsException(f"LLM调用失败: {str(exc)}") from exc
        finally:
            if stream_completed and not usage_recorded:
                self._record_fallback_usage(messages, "".join(output_parts))

    def _include_stream_usage(self) -> bool:
        if self._stream_usage_mode == "enabled":
            return True
        if self._stream_usage_mode == "disabled":
            return False
        base_url = str(self.base_url or "").lower()
        return self.provider in {"openai", "deepseek"} or any(
            host in base_url for host in ("api.openai.com", "api.deepseek.com")
        )

    def _record_provider_usage(self, usage: Any) -> bool:
        if usage is None:
            return False
        try:
            prompt = self._usage_value(usage, "prompt_tokens")
            completion = self._usage_value(usage, "completion_tokens")
            total = self._usage_value(usage, "total_tokens")
            if prompt is None or completion is None:
                return False
            self._recorder.record_llm_usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                source="provider",
            )
            return True
        except Exception as exc:
            self._recorder.add_warning(f"provider_usage_parse_failed:{type(exc).__name__}")
            return False

    def _record_fallback_usage(
        self, messages: list[dict[str, str]], output: str
    ) -> None:
        if self._token_usage_fallback != "estimated":
            return
        try:
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except KeyError:
                encoding = tiktoken.get_encoding("cl100k_base")
            prompt_text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            self._recorder.record_llm_usage(
                prompt_tokens=len(encoding.encode(prompt_text)),
                completion_tokens=len(encoding.encode(output)),
                source="estimated",
            )
        except Exception as exc:
            self._recorder.add_warning(f"token_estimation_unavailable:{type(exc).__name__}")

    @staticmethod
    def _usage_value(usage: Any, field: str) -> int | None:
        value = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
        return int(value) if value is not None else None
