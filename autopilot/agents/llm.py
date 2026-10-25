"""Provider-agnostic agent loop over any OpenAI-compatible chat-completions endpoint.

Configured by LLM_BASE_URL / LLM_API_KEY / LLM_MODEL (default: Alibaba Model Studio Coding Plan with
qwen3.7-plus). Tools are plain Python functions with a hand-written JSON schema; the loop runs until a
*terminal* tool is called, the model stops calling tools, or the call/time caps are hit."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from openai import OpenAI

from autopilot import config, ui
from autopilot.agents.sqlguard import cap

MAX_TOOL_CALLS = 25
MAX_SECONDS = 300


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]
    terminal: bool = False

    def schema(self) -> dict[str, Any]:
        params = dict(self.parameters)
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": params}}


@dataclass
class AgentResult:
    finished_by: str  # terminal_tool | end_turn | max_tool_calls | timeout | error
    messages: list[dict[str, Any]]
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    final_text: str = ""
    terminal_result: str | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    model: str = ""


def client() -> OpenAI:
    if not config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set (see .env.example)")
    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=120, max_retries=2)


def _assistant_dict(msg) -> dict[str, Any]:
    """Only echo back the fields the API expects; drop provider-specific extras (reasoning etc.)."""
    d: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls]
    return d


def run_agent(agent_name: str, system: str, user: str, tools: list[Tool],
              max_tool_calls: int = MAX_TOOL_CALLS, max_seconds: int = MAX_SECONDS) -> AgentResult:
    oai = client()
    model = config.LLM_MODEL
    by_name = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools]
    terminal_names = [t.name for t in tools if t.terminal]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    result = AgentResult(finished_by="error", messages=messages, model=model)
    ui.agent_header(agent_name, model)
    t0 = time.time()
    nudged = False

    while True:
        if result.tool_calls >= max_tool_calls:
            result.finished_by = "max_tool_calls"; break
        if time.time() - t0 > max_seconds:
            result.finished_by = "timeout"; break
        resp = oai.chat.completions.create(model=model, messages=messages, tools=schemas, tool_choice="auto")
        if resp.usage:
            result.input_tokens += resp.usage.prompt_tokens or 0
            result.output_tokens += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message
        messages.append(_assistant_dict(msg))
        if msg.content:
            ui.assistant_text(msg.content)

        if not msg.tool_calls:
            if terminal_names and not nudged:
                nudged = True
                messages.append({"role": "user", "content":
                                 f"You have not finished. Finish now by calling `{terminal_names[0]}` with your conclusions."})
                continue
            result.finished_by = "end_turn"
            result.final_text = msg.content or ""
            break

        tool_msgs: list[dict[str, Any]] = []
        hit_terminal: str | None = None
        for tc in msg.tool_calls:
            result.tool_calls += 1
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except (json.JSONDecodeError, ValueError) as e:
                tool_msgs.append({"role": "tool", "tool_call_id": tc.id, "content": f"ERROR: invalid JSON arguments ({e}). Retry."})
                continue
            ui.tool_call(name, args)
            tool = by_name.get(name)
            if tool is None:
                out = f"ERROR: unknown tool '{name}'. Available: {', '.join(by_name)}"
            else:
                try:
                    out = str(tool.fn(**args))
                except TypeError as e:
                    out = f"ERROR: bad arguments for {name}: {e}"
                except Exception as e:  # tool errors go back to the model, never crash the loop
                    out = f"ERROR: {type(e).__name__}: {e}"
            out = cap(out)
            ui.tool_result(out)
            tool_msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
            if tool is not None and tool.terminal and not out.startswith("ERROR"):
                hit_terminal = out
        messages.extend(tool_msgs)  # all results for the turn go back together
        if hit_terminal is not None:
            result.finished_by = "terminal_tool"
            result.terminal_result = hit_terminal
            break

    result.messages = messages
    ui.info(f"{agent_name} finished: {result.finished_by} after {result.tool_calls} tool calls, "
            f"{result.input_tokens} in / {result.output_tokens} out tokens, {time.time() - t0:.0f}s")
    return result
