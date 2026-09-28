"""Claude client for one decision per step. The only module that imports the Anthropic SDK."""

from __future__ import annotations

import os
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel

_NUDGE = "Respond by calling exactly one of the tools."
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """The model could not produce a usable decision (API failure, refusal, no tool call)."""


class LLMDecision(BaseModel):
    tool: str
    input: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    fell_back: bool = False  # a safety classifier declined on the requested model; a fallback model answered


class Decider(Protocol):
    def decide(self, system: str, content: list[dict], tools: list[dict], timeout_s: float) -> LLMDecision: ...


class ClaudeDecider:
    def __init__(self, model: str | None = None, client: anthropic.Anthropic | None = None, max_tokens: int = 16000):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "")
        if not self.model:
            raise LLMError("ANTHROPIC_MODEL is not set")
        self.client = client or anthropic.Anthropic()
        self.max_tokens = max_tokens

    def decide(self, system: str, content: list[dict], tools: list[dict], timeout_s: float) -> LLMDecision:
        # Each step is one fresh, self-contained request: history lives in our own step log, not in a
        # growing transcript. The only multi-turn case is one nudge when no tool was called (append-only).
        messages: list[dict] = [{"role": "user", "content": content}]
        for _ in range(2):
            response = self._create(system, messages, tools, timeout_s)
            if response.stop_reason == "refusal":
                # Only reached when the fallback model declined too (or no fallback was available).
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) or "unspecified"
                explanation = getattr(details, "explanation", None)
                raise LLMError(f"model declined the request (category: {category})"
                               + (f": {explanation}" if explanation else ""))
            if response.stop_reason == "max_tokens":
                raise LLMError("model response was cut off (max_tokens)")
            calls = [b for b in response.content if b.type == "tool_use"]
            if len(calls) == 1:
                # `response.model` is the model that actually answered, which differs from
                # self.model when a safety-classifier decline was rerouted to the fallback.
                return LLMDecision(
                    tool=calls[0].name, input=dict(calls[0].input), model=response.model,
                    input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
                    fell_back=any(b.type == "fallback" for b in response.content),
                )
            messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": _NUDGE}]
        raise LLMError("model did not call a tool")

    def _create(self, system: str, messages: list[dict], tools: list[dict], timeout_s: float):
        try:
            return self.client.with_options(timeout=max(timeout_s, 1.0)).beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Safety classifiers can decline benign requests (a login form reads like credential
                # abuse to a cyber classifier). "default" reruns a decline on the model Anthropic
                # recommends for that category, server-side; the served model is logged per step.
                betas=[_FALLBACK_BETA],
                fallbacks="default",
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                # `auto` + prompt instruction instead of forced `any`: some current models reject forced tool use.
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=messages,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
            raise LLMError(f"LLM configuration error: {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"LLM rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("LLM rate limited (after SDK retries)") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"LLM API error {exc.status_code} (after SDK retries)") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("LLM unreachable or timed out (after SDK retries)") from exc
