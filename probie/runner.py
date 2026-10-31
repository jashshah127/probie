"""Run one model over the cases and write its results.

Results go to runs/<model name>.jsonl, one line per case, written as each case
finishes. Running the same command again picks up where it stopped. If the model,
prompts or cases change, the old results no longer describe this setup, so the run
refuses to mix them unless you start fresh (the old file is kept as a backup).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from probie import agent
from probie import cases as case_views
from probie import documents
from probie import ledger
from probie import voicemails
from probie import tools as case_tools
from probie.budget import Budget, BudgetExceeded, Prices
from probie.cache import ResponseCache
from probie.config import Config, ModelSpec
from probie.offline import OfflineClient
from probie.providers import ChatClient, HTTPChatClient, ProviderError, RateLimiter

RESULTS_KIND = "probie results"
FINAL_STATUSES = ("decided", "unusable")
MAX_CONSECUTIVE_FAILURES = 5
# A model can answer every time and still never give a usable decision: wrong tool, no amount,
# unparseable JSON. That costs money and produces nothing, so a run gives up early on it too.
MAX_CONSECUTIVE_UNUSABLE = 12


class RunError(RuntimeError):
    pass


def _expense_prompts() -> list:
    from probie import expenses
    return [expenses.SYSTEM_AGENT, expenses.SYSTEM_AGENT_DOCUMENT, expenses.SYSTEM_PROMPT, expenses.LOOKUP_TOOLS,
            expenses.ZOOM_TOOL]


def prompt_fingerprint() -> str:
    material = json.dumps({
        "system_agent": case_views.SYSTEM_AGENT,
        "system_agent_document": case_views.SYSTEM_AGENT_DOCUMENT,
        "system_prompt": case_views.SYSTEM_PROMPT,
        "tools": case_tools.TOOL_SCHEMAS,
        "document_tools": case_tools.DOCUMENT_TOOL_SCHEMAS,
        "expenses": _expense_prompts(),
        "packet_version": case_views.PACKET_VERSION,
        "nudges": [agent.NUDGE_AGENT, agent.NUDGE_PROMPT],
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def run_signature(spec: ModelSpec, cases_sha256: str, documents_fingerprint: str | None = None,
                  voicemail_fingerprint: str | None = None) -> str:
    material = json.dumps({
        "voicemail_input": spec.voicemail_input,
        "voicemails": voicemail_fingerprint,
        "invoice_input": spec.invoice_input,
        "documents": documents_fingerprint,
        "provider": spec.provider,
        "base_url": spec.base_url,
        "model": spec.model,
        "offline_profile": spec.offline_profile,
        "mode": spec.mode,
        "max_tokens": spec.max_tokens,
        "max_tokens_param": spec.max_tokens_param,
        "params": spec.params,
        "json_mode": spec.json_mode,
        "prompts": prompt_fingerprint(),
        "cases": cases_sha256,
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


ONLY = ("voicemails", "redteam", "ap", "expenses")


def choose(cases: list[dict], *, sample: int | None = None, only: str | None = None) -> list[dict]:
    """A subset to run when the budget won't stretch to everything.

    sample: that many cases from the month, an equal share of each duty, spread across the month,
            plus every red-team attack. The same sample every time.
    only:   "voicemails" (the cases with a voicemail), "redteam" (the attack wave), or one department:
            "ap" or "expenses". It can be combined with sample, which then samples within it.
    """
    if only in ("ap", "expenses"):
        cases = [c for c in cases if c.get("department", "ap") == only]
        only = None
    if only is not None:
        if only not in ONLY:
            raise RunError(f"--only takes one of: {', '.join(ONLY)}")
        key = "voicemail" if only == "voicemails" else "redteam"
        return [c for c in cases if c.get(key)]
    if sample is None:
        return list(cases)
    month = [c for c in cases if not c.get("redteam")]
    duties = sorted({c["type"] for c in month})
    share, extra = divmod(sample, len(duties))
    picked = []
    for i, duty in enumerate(duties):
        pool = sorted((c for c in month if c["type"] == duty), key=lambda c: (c["invoice"]["date"], c["id"]))
        k = min(len(pool), share + (1 if i < extra else 0))
        if k:
            step = len(pool) / k
            picked += [pool[int(j * step)] for j in range(k)]
    ids = {c["id"] for c in picked} | {c["id"] for c in cases if c.get("redteam")}
    return [c for c in cases if c["id"] in ids]


def prices_for(spec: ModelSpec) -> Prices:
    return Prices(spec.price_input, spec.price_cached_input, spec.price_output, spec.price_per_hour)


def results_path(config: Config, spec: ModelSpec, repeat: int = 1) -> Path:
    """runs/<name>.jsonl, and runs/<name>@2.jsonl for a second pass that measures consistency."""
    suffix = "" if repeat == 1 else f"@{repeat}"
    return config.run.runs_dir / f"{spec.name}{suffix}.jsonl"


@dataclass
class ResultsFile:
    header: dict | None
    results: dict[str, dict]
    sessions: list[dict]


def read_results(path: Path) -> ResultsFile:
    if not path.exists():
        return ResultsFile(None, {}, [])
    header = None
    results: dict[str, dict] = {}
    sessions: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A run killed mid-write can leave a partial last line. Skip it; that case reruns.
                continue
            if "_header" in record:
                header = record["_header"]
            elif "_session" in record:
                sessions.append(record["_session"])
            elif isinstance(record.get("case_id"), str):
                results[record["case_id"]] = record
    if header is None or header.get("kind") != RESULTS_KIND:
        raise RunError(f"{path} isn't a Probie results file")
    return ResultsFile(header, results, sessions)


def build_client(spec: ModelSpec, cases: list[dict]) -> ChatClient:
    if spec.offline:
        return OfflineClient(cases, spec.offline_profile)
    key = spec.api_key()
    if spec.api_key_env and not key:
        raise RunError(f"{spec.name} needs an API key in the {spec.api_key_env} environment variable (or in .env).")
    return HTTPChatClient(spec.base_url, key, timeout=spec.timeout, rate_limiter=RateLimiter(spec.rpm))


@dataclass
class RunSummary:
    model: str
    path: Path
    todo: int
    completed: int = 0
    decided: int = 0
    unusable: int = 0
    errors: int = 0
    spent_usd: float = 0.0
    stopped: str | None = None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_model(
    config: Config,
    spec: ModelSpec,
    cases: list[dict],
    cases_sha256: str,
    *,
    limit: int | None = None,
    budget_usd: float | None = None,
    concurrency: int | None = None,
    fresh: bool = False,
    use_cache: bool = True,
    past_credits: bool = False,
    repeat: int = 1,
    only_ids: set[str] | None = None,
    client: ChatClient | None = None,
    out: Callable[[str], None] = print,
) -> RunSummary:
    prices = prices_for(spec)
    if not prices.known:
        raise RunError(
            f"{spec.name} has no prices, so the spend cap can't work. Set price_input and price_output "
            "(USD per million tokens) or price_per_hour in the config."
        )
    path = results_path(config, spec, repeat)
    path.parent.mkdir(parents=True, exist_ok=True)
    if repeat > 1:
        use_cache = False  # a second pass has to ask the model again
    # Only the cases this run will actually work need their paperwork ready: a sample that contains
    # no voicemail cases shouldn't be blocked by voicemails that haven't been recorded yet.
    selected = cases if only_ids is None else [case for case in cases if case["id"] in only_ids]
    fingerprint = None
    if spec.invoice_input == "document":
        ids = [case["id"] for case in selected]
        missing = documents.missing_documents(config.run.documents, ids)
        if missing:
            raise RunError(f"{spec.name} reads invoice images, and {len(missing)} aren't rendered yet. "
                           "Run: python -m probie render")
        fingerprint = documents.manifest_fingerprint(documents.load_manifest(config.run.documents),
                                                     [case["id"] for case in cases])
    voicemail_fingerprint = None
    if spec.voicemail_input != "script" and voicemails.voicemail_cases(selected):
        absent = voicemails.missing(selected, config.run.voicemails, spec.voicemail_input)
        if absent:
            raise RunError(f"{spec.name} hears voicemails as {spec.voicemail_input}, and {len(absent)} aren't ready "
                           f"({', '.join(absent[:4])}{'...' if len(absent) > 4 else ''}). Record them, then run: "
                           "python -m probie voicemails --transcribe")
        voicemail_fingerprint = voicemails.fingerprint(cases, config.run.voicemails, spec.voicemail_input)
    signature = run_signature(spec, cases_sha256, fingerprint, voicemail_fingerprint)

    existing = read_results(path)
    if existing.header is not None and (fresh or existing.header.get("signature") != signature):
        if not fresh:
            raise RunError(
                f"{path} holds results from a different setup (the model, its settings, the prompts or the "
                "cases changed). Run again with --fresh to start over; the old file is kept as a backup."
            )
        backup = path.with_name(f"{path.stem}.{dt.datetime.now():%Y%m%d-%H%M%S}.bak.jsonl")
        path.rename(backup)
        out(f"Moved the old results to {backup.name}.")
        existing = ResultsFile(None, {}, [])

    done = {cid for cid, r in existing.results.items() if r.get("status") in FINAL_STATUSES}
    todo = [case for case in cases if case["id"] not in done and (only_ids is None or case["id"] in only_ids)]
    if limit is not None:
        todo = todo[:limit]
    summary = RunSummary(spec.name, path, len(todo))
    if not todo:
        out(f"{spec.name}: every case already has a result.")
        return summary

    cap = budget_usd or config.run.budget_usd
    credit_pool = ledger.pool_for(spec)
    overall = ledger.total_left(config.credit_total, config.credit_margin, config.run.runs_dir)
    if credit_pool is not None and overall is not None and not past_credits:
        if overall <= 0:
            raise RunError(f"The ${config.credit_total:.2f} limit on all API spend is reached. "
                           "Add --past-credits to go past it.")
        if overall < cap:
            cap = overall
            out(f"{spec.name}: capped at ${overall:.2f}, what's left under the ${config.credit_total:.2f} limit on all API spend.")
    if credit_pool is not None and credit_pool in config.credits and not past_credits:
        left = ledger.usable(config.credits, config.credit_margin, config.run.runs_dir)[credit_pool]
        if left <= 0:
            raise RunError(f"The {credit_pool} credits are used up. Add --past-credits to keep going on your own card.")
        if left < cap:
            cap = left
            out(f"{spec.name}: capped at ${left:.2f}, what's left of the {credit_pool} credits.")
    client = client or build_client(spec, cases)
    budget = Budget(cap, per_hour=prices.per_hour)
    cache = ResponseCache(config.run.cache) if use_cache else None
    namespace = f"offline:{spec.offline_profile}" if spec.offline else str(spec.base_url)
    ctx = agent.CallContext(spec, client, budget, prices, cache, namespace, config.run.documents, config.run.voicemails)
    workers = concurrency or spec.concurrency or config.run.concurrency
    stop = threading.Event()
    lock = threading.Lock()
    started = time.monotonic()
    started_at = _now()
    consecutive_failures = 0
    consecutive_unusable = 0
    progress_every = max(1, len(todo) // 20)
    internal_error: BaseException | None = None

    handle = path.open("a", encoding="utf-8", newline="\n")
    try:
        if existing.header is None:
            header = {"kind": RESULTS_KIND, "model": spec.name, "signature": signature, "cases_sha256": cases_sha256,
                      "mode": spec.mode, "invoice_input": spec.invoice_input, "offline": spec.offline,
                      "created_at": started_at}
            handle.write(json.dumps({"_header": header}) + "\n")
            handle.flush()

        def write(record: dict) -> None:
            with lock:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()

        def accept(result: dict) -> None:
            nonlocal consecutive_unusable
            write(result)
            summary.completed += 1
            if result["status"] == "decided":
                summary.decided += 1
                consecutive_unusable = 0
                return
            summary.unusable += 1
            consecutive_unusable += 1
            if consecutive_unusable >= MAX_CONSECUTIVE_UNUSABLE and not stop.is_set():
                why = result.get("detail") or result.get("unusable") or "no usable decision"
                summary.stopped = (f"{consecutive_unusable} cases in a row gave no usable answer; last: {why}. "
                                   f"Check it with: python -m probie ping --model {spec.name}")
                stop.set()

        def work(case: dict) -> dict:
            return agent.run_case(case, ctx, config.run.max_steps)

        pending = iter(todo)
        futures: dict[Future, dict] = {}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            def submit_next() -> None:
                if stop.is_set():
                    return
                case = next(pending, None)
                if case is not None:
                    futures[pool.submit(work, case)] = case

            for _ in range(workers):
                submit_next()
            try:
                while futures:
                    finished, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                    for future in finished:
                        case = futures.pop(future)
                        try:
                            result = future.result()
                        except BudgetExceeded as exc:
                            if not stop.is_set():
                                summary.stopped = f"spend cap reached: {exc}"
                            stop.set()
                            continue
                        except ProviderError as exc:
                            if not exc.retryable:
                                if not stop.is_set():
                                    summary.stopped = f"the provider refused the request: {exc}"
                                stop.set()
                                continue
                            consecutive_failures += 1
                            summary.errors += 1
                            write({"case_id": case["id"], "status": "error", "detail": str(exc)})
                            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not stop.is_set():
                                summary.stopped = f"{consecutive_failures} requests in a row failed; last error: {exc}"
                                stop.set()
                            submit_next()
                            continue
                        except Exception as exc:  # a bug: stop cleanly, then report it
                            if internal_error is None:
                                internal_error = exc
                                summary.stopped = f"internal error on {case['id']}: {exc!r}"
                            stop.set()
                            continue
                        consecutive_failures = 0
                        accept(result)
                        if summary.completed % progress_every == 0 or summary.completed == len(todo):
                            out(f"{spec.name}: {summary.completed}/{len(todo)} done, ${budget.spent:.4f} spent "
                                f"of the ${budget.cap:.2f} cap")
                        submit_next()
            except KeyboardInterrupt:
                stop.set()
                summary.stopped = "interrupted"
                out("Stopping after the requests in flight finish. Run the same command to pick up from here.")
                for future in list(futures):
                    try:
                        accept(future.result())
                    except Exception:
                        continue
                futures.clear()
    finally:
        summary.spent_usd = budget.spent
        session = {"started_at": started_at, "elapsed_s": round(time.monotonic() - started, 3),
                   "spent_usd": round(summary.spent_usd, 6), "completed": summary.completed,
                   "stopped": summary.stopped}
        handle.write(json.dumps({"_session": session}) + "\n")
        handle.close()
        ledger.record(config.run.runs_dir, pool=credit_pool, model=spec.name, usd=summary.spent_usd, what="run")
        if cache is not None:
            cache.close()

    if internal_error is not None:
        raise RunError(f"{spec.name} stopped on an internal error: {internal_error!r}") from internal_error
    return summary
