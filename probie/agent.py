"""Run one case through a model.

Agent mode: the model sees the flag, the invoice and the vendor's message, looks
records up with tools, and calls submit_decision. We record which checks it made
before deciding. A lookup made in the same response as the decision doesn't count,
because the model couldn't have read the result.

Prompt mode: the model gets every record in one message and replies with JSON.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from pathlib import Path

from probie import cases as case_views
from probie import decision as dec
from probie import documents
from probie import voicemails
from probie import tools as case_tools
from probie.budget import Budget, Prices, Usage, extract_usage
from probie.cache import ResponseCache, request_key
from probie.config import ModelSpec
from probie.providers import ChatClient

TRUNCATE = 1200
NUDGE_AGENT = "Call submit_decision to record your decision."
NUDGE_PROMPT = "Reply with only the JSON object."


def _truncate(text: str | None, limit: int = TRUNCATE) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "..."


def _content_text(content: object) -> str:
    """Message content can be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


@dataclass
class CallInfo:
    usage: Usage
    cost: float
    latency: float
    cached: bool


class CallContext:
    """Makes model calls for one model: cache first, then budget, then the provider."""

    def __init__(self, spec: ModelSpec, client: ChatClient, budget: Budget, prices: Prices,
                 cache: ResponseCache | None, namespace: str, documents_dir: Path | None = None,
                 voicemails_dir: Path | None = None):
        self.spec = spec
        self.client = client
        self.budget = budget
        self.prices = prices
        self.cache = cache
        self.namespace = namespace
        self.documents_dir = documents_dir
        self.voicemails_dir = voicemails_dir
        self.live_steps: list | None = None  # when set, steps are written here as they happen

    def voicemail(self, case: dict) -> dict | None:
        """The case's voicemail as this model hears it: a transcript, the audio, or the script."""
        if not case.get("voicemail"):
            return None
        mode = self.spec.voicemail_input
        if mode != "script" and self.voicemails_dir is None:
            raise voicemails.VoicemailError("this model needs a voicemails folder")
        return voicemails.heard(case, self.voicemails_dir, mode)

    def invoice_image(self, case: dict) -> str | None:
        """The invoice as an image data URL in document mode; None when the model gets fields."""
        if self.spec.invoice_input != "document":
            return None
        if self.documents_dir is None:
            raise documents.DocumentError("document mode needs a documents folder")
        return documents.image_data_url(self.documents_dir, case["id"])

    @property
    def reads_documents(self) -> bool:
        return self.spec.invoice_input == "document"

    def payload(self, messages: list[dict], *, tools: bool, json_mode: bool = False, case: dict | None = None) -> dict:
        payload: dict = {"model": self.spec.model, "messages": list(messages)}
        if tools:
            payload["tools"] = case_tools.schemas_for(case or {}, self.reads_documents)
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload[self.spec.max_tokens_param] = self.spec.max_tokens
        payload.update(self.spec.params)
        return payload

    def call(self, payload: dict) -> tuple[dict, CallInfo]:
        key = request_key(self.namespace, payload) if self.cache is not None else None
        if key is not None:
            hit = self.cache.get(key)
            if hit is not None:
                usage = extract_usage(hit)
                return hit, CallInfo(usage, self.prices.cost(usage), 0.0, True)
        token = self.budget.reserve(self.prices.worst_case(payload, self.spec.max_tokens))
        started = time.monotonic()
        try:
            response = self.client.chat(payload)
        except BaseException:
            self.budget.release(token)
            raise
        latency = time.monotonic() - started
        usage = extract_usage(response)
        cost = self.prices.cost(usage)
        self.budget.settle(token, cost)
        if key is not None:
            self.cache.put(key, response)
        return response, CallInfo(usage, cost, latency, False)


@dataclass
class _Tally:
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    latency: float = 0.0
    calls: int = 0
    cached_calls: int = 0
    steps: list[dict] = field(default_factory=list)

    def add(self, info: CallInfo) -> None:
        self.usage = self.usage + info.usage
        self.cost += info.cost
        self.latency += info.latency
        self.calls += 1
        self.cached_calls += int(info.cached)


def _result(case: dict, tally: _Tally, *, status: str, decision: dec.Decision | None = None,
            unusable: str | None = None, detail: str | None = None, checks: set[str] | None = None,
            agent: bool, zooms: list[str] | None = None) -> dict:
    return {
        "zooms": zooms or [],
        "case_id": case["id"],
        "status": status,
        "decision": decision.decision if decision else None,
        "amount_cents": decision.amount_cents if decision else None,
        "rule": decision.rule if decision else None,
        "reason": decision.reason if decision else None,
        "note": (decision.note or None) if decision else None,
        "unusable": unusable,
        "detail": detail,
        "checks": sorted(checks) if (agent and checks is not None) else None,
        "calls": tally.calls,
        "cached_calls": tally.cached_calls,
        "usage": tally.usage.as_dict(),
        "cost_usd": round(tally.cost, 8),
        "latency_s": round(tally.latency, 3),
        "steps": tally.steps,
    }


def _normalize_tool_calls(raw: list, step: int) -> list[dict]:
    calls = []
    for i, call in enumerate(raw):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {})
        calls.append({
            "id": call.get("id") or f"call_{step}_{i}",
            "type": "function",
            "function": {"name": str(function.get("name") or ""), "arguments": arguments},
        })
    return calls


def run_agent(case: dict, ctx: CallContext, max_steps: int) -> dict:
    total = case["invoice"]["total_cents"]
    lookups = case_tools.tools_for(case)
    messages: list[dict] = [
        {"role": "system", "content": case_views.system_for(case, agent=True, documents=ctx.reads_documents)},
        {"role": "user", "content": case_views.agent_content(case, ctx.invoice_image(case), ctx.voicemail(case))},
    ]
    tally = _Tally()
    if ctx.live_steps is not None:
        tally.steps = ctx.live_steps
    checks: set[str] = set()
    nudged = False
    last_invalid: dec.InvalidDecision | None = None
    zooms: list[str] = []

    for step in range(max_steps):
        response, info = ctx.call(ctx.payload(messages, tools=True, case=case))
        tally.add(info)
        choice = response["choices"][0]
        message = choice["message"]
        text = _content_text(message.get("content"))
        calls = _normalize_tool_calls(message.get("tool_calls") or [], step)
        record = {"content": _truncate(text), "tool_calls": []}
        tally.steps.append(record)

        if calls:
            messages.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            decided: dec.Decision | None = None
            new_checks: set[str] = set()
            views: list[tuple[str, str]] = []
            for call in calls:
                name = call["function"]["name"]
                raw_args = call["function"]["arguments"]
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = None
                entry = {"name": name, "args": args if isinstance(args, dict) else _truncate(raw_args, 200)}
                if not isinstance(args, dict):
                    reply = "Error: the arguments weren't a valid JSON object."
                    entry["ok"] = False
                elif name == "submit_decision":
                    if decided is not None:
                        reply = "Already recorded."
                        entry["ok"] = False
                    else:
                        try:
                            decided = dec.from_fields(args, total)
                        except dec.InvalidDecision as exc:
                            last_invalid = exc
                            reply = f"Error: {exc.message}. Call submit_decision again with a valid decision."
                            entry["ok"] = False
                        else:
                            reply = "Recorded."
                            entry["ok"] = True
                elif name in case_tools.ZOOM_NAMES and ctx.reads_documents:
                    region = args.get("region")
                    allowed = case_tools.zoom_regions(case)
                    url = documents.region_data_url(ctx.documents_dir, case["id"], region) if region in allowed else None
                    if url is None:
                        reply = f"Error: region must be one of {', '.join(allowed)}."
                        entry["ok"] = False
                    else:
                        views.append((region, url))
                        zooms.append(region)
                        reply = f"The zoomed {region} view is attached in the next message."
                        entry["ok"] = True
                else:
                    reply, check = lookups.execute(name, args)
                    entry["ok"] = check is not None
                    if check:
                        new_checks.add(check)
                record["tool_calls"].append(entry)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": reply})
            if decided is not None:
                return _result(case, tally, status="decided", decision=decided, checks=checks, agent=True, zooms=zooms)
            checks |= new_checks
            if views:
                parts: list = [{"type": "text", "text": "Zoomed views you asked for:"}]
                for region, url in views:
                    parts += [{"type": "text", "text": f"{region}:"}, {"type": "image_url", "image_url": {"url": url}}]
                messages.append({"role": "user", "content": parts})
            continue

        try:
            decided = dec.parse_text(text, total)
        except dec.InvalidDecision as exc:
            return _result(case, tally, status="unusable", unusable=exc.code, detail=exc.message,
                           checks=checks, agent=True)
        if decided is not None:
            return _result(case, tally, status="decided", decision=decided, checks=checks, agent=True, zooms=zooms)
        if choice.get("finish_reason") == "length":
            return _result(case, tally, status="unusable", unusable="length",
                           detail="ran out of output tokens before deciding", checks=checks, agent=True, zooms=zooms)
        if nudged:
            return _result(case, tally, status="unusable", unusable="no_decision",
                           detail="answered without a decision, even after a reminder", checks=checks, agent=True)
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": NUDGE_AGENT})
        nudged = True

    if last_invalid is not None:
        return _result(case, tally, status="unusable", unusable=last_invalid.code, detail=last_invalid.message,
                       checks=checks, agent=True)
    return _result(case, tally, status="unusable", unusable="step_limit",
                   detail=f"no decision within {max_steps} steps", checks=checks, agent=True)


def run_prompt(case: dict, ctx: CallContext) -> dict:
    total = case["invoice"]["total_cents"]
    messages: list[dict] = [
        {"role": "system", "content": case_views.system_for(case, agent=False, documents=ctx.reads_documents)},
        {"role": "user", "content": case_views.prompt_content(case, ctx.invoice_image(case), ctx.voicemail(case))},
    ]
    tally = _Tally()
    if ctx.live_steps is not None:
        tally.steps = ctx.live_steps
    for attempt in range(2):
        response, info = ctx.call(ctx.payload(messages, tools=False, json_mode=ctx.spec.json_mode))
        tally.add(info)
        choice = response["choices"][0]
        text = _content_text(choice["message"].get("content"))
        tally.steps.append({"content": _truncate(text), "tool_calls": []})
        try:
            decided = dec.parse_text(text, total)
        except dec.InvalidDecision as exc:
            return _result(case, tally, status="unusable", unusable=exc.code, detail=exc.message, agent=False)
        if decided is not None:
            return _result(case, tally, status="decided", decision=decided, agent=False)
        if choice.get("finish_reason") == "length":
            return _result(case, tally, status="unusable", unusable="length",
                           detail="ran out of output tokens before answering", agent=False)
        if attempt == 0:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": NUDGE_PROMPT})
    return _result(case, tally, status="unusable", unusable="no_decision",
                   detail="no JSON decision, even after a reminder", agent=False)


def run_case(case: dict, ctx: CallContext, max_steps: int) -> dict:
    if ctx.spec.mode == "agent":
        return run_agent(case, ctx, max_steps)
    return run_prompt(case, ctx)
