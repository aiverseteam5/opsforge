"""ModelGateway — the single boundary for all LLM I/O.

Provider-agnostic by design: callers only ever use `chat()` / `embedding()`,
and the concrete model is a config string routed by litellm to cloud, private
OpenAI-compatible, or local (Ollama/vLLM/LM Studio) backends. Swapping models is
configuration, never code.

Messages and tool schemas use the OpenAI chat shape (what litellm consumes);
results are normalized so the agent loop never sees provider-specific objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class ModelGateway(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> ChatResult: ...

    async def embedding(self, texts: list[str], model: str) -> list[list[float]]: ...


@dataclass
class LiteLLMGateway:
    """The one production implementation. `model` is any litellm model string (the
    provider is the prefix: openai/…, azure/…, anthropic/…, bedrock/…, ollama/…).

    `api_key` / `api_base` are the per-workspace credential resolved from the vault at
    call time (M7.6 Job A); when both are None, litellm falls back to environment
    variables — the LOCAL-DEV-ONLY path, never the deployed one."""

    api_key: str | None = None
    api_base: str | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> ChatResult:
        import litellm

        resp = await litellm.acompletion(
            model=model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            api_key=self.api_key,
            api_base=self.api_base,
        )
        msg = resp.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
        }
        return ChatResult(text=msg.content, tool_calls=tool_calls, usage=usage)

    async def embedding(self, texts: list[str], model: str) -> list[list[float]]:
        import litellm

        resp = await litellm.aembedding(model=model, input=texts)
        return [item["embedding"] for item in resp.data]


def make_assistant_message(result: ChatResult) -> dict[str, Any]:
    """Reconstruct the OpenAI-shaped assistant message for the next turn."""
    msg: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
    if result.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in result.tool_calls
        ]
    return msg


def make_tool_message(tool_call: ToolCall, output: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "name": tool_call.name,
        "content": output if isinstance(output, str) else json.dumps(output),
    }
