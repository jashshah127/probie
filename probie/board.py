"""The clearance board: a page that replays the month through the desk. It's the live demo."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from probie import __version__
from probie.config import ModelSpec
from probie.report import _embed, _write_atomic

PLACEHOLDER = "__PROBIE_BOARD__"


def _detail(case: dict, specs: list[ModelSpec], results: dict[str, dict[str, dict]], document: str | None,
            transcripts: dict | None) -> dict:
    invoice = case["invoice"]
    email = case.get("email")
    vm = case.get("voicemail")
    return {
        "id": case["id"], "vendor": invoice["vendor_name"], "total": invoice["total_cents"], "duty": case["type"],
        "number": invoice["number"], "kind": "Expense claim" if case.get("department") == "expenses" else "Invoice",
        "lines": [{"description": l["description"], "qty": l["qty"], "amount": l["amount_cents"]} for l in invoice["lines"]],
        "remit": f"{invoice['remit_to']['bank_name']}, routing {invoice['remit_to']['routing']}, account {invoice['remit_to']['account']}",
        "redteam": case.get("redteam"),
        "flag": case["flag"], "message": case["message"], "document": document,
        "email": {"from": f"{email['from_name']} <{email['from_address']}>", "reply_to": email.get("reply_to"),
                  "subject": email["subject"]} if email else None,
        "voicemail": {"caller": vm["caller"], "text": (transcripts or {}).get(vm["id"]) or vm["script"]} if vm else None,
        "answer": {k: case["answer"][k] for k in ("decision", "amount_cents", "why")},
        "results": {s.name: {k: results[s.name][case["id"]].get(k) for k in ("decision", "amount_cents", "reason")}
                    for s in specs if case["id"] in results.get(s.name, {})},
    }


def build_board_data(month: dict, cases_meta: dict, cases: list[dict], specs: list[ModelSpec],
                     results: dict[str, dict[str, dict]], document_links: dict[str, dict] | None = None,
                     transcripts: dict | None = None, portraits: dict[str, str] | None = None) -> dict:
    by_id = {c["id"]: c for c in cases}
    spec_by = {s.name: s for s in specs}
    cost = month["cost_per_case"]
    order = sorted(month["models"], key=lambda n: (cost.get(n) is None, -(cost.get(n) or 0.0)))
    used = [spec_by[n] for n in month["models"]]
    duty_keys = [k for k in cases_meta["types"] if k in month["duties"]]
    links = document_links or {}
    event_cases = {e["case_id"] for e in month["events"]}
    return {
        "meta": {"version": __version__, "company": cases_meta.get("company"), "invoices": len(month["routing"]),
                 "offline_present": any(spec_by[n].offline for n in month["models"])},
        "models": [{"name": n, "model": spec_by[n].model, "note": spec_by[n].note, "offline": spec_by[n].offline,
                    "cost_per_case": cost.get(n)} for n in order],
        "shadow_order": month["models"],
        "duties": [{"key": k, "label": cases_meta["types"][k]["label"],
                    "department": cases_meta["types"][k].get("department", "ap")} for k in duty_keys],
        "departments": cases_meta.get("departments") or {"ap": "Accounts payable"},
        "threshold": month["settings"]["clean_decisions_needed"],
        "portraits": portraits or {},
        "settings": month["settings"],
        "baseline_model": month["baseline_model"],
        "summary": month["summary"],
        "days": month["days"],
        "events": month["events"],
        "invoices": [{"id": r["case_id"], "date": r["date"], "duty": r["duty"],
                      "vendor": by_id[r["case_id"]]["invoice"]["vendor_name"],
                      "total": by_id[r["case_id"]]["invoice"]["total_cents"], "to": r["to"], "outcome": r["outcome"],
                      "delta": r["delta_cents"], "baseline_delta": r["baseline_delta_cents"],
                      "cost": round(r["cost_usd"], 8), "shadow": r["shadow"],
                      "redteam": by_id[r["case_id"]].get("redteam")} for r in month["routing"]],
        "cases": {cid: _detail(by_id[cid], used, results, (links.get(cid) or {}).get("src"), transcripts)
                  for cid in sorted(event_cases)},
    }


def write_board(path: str | Path, data: dict) -> None:
    template = resources.files("probie").joinpath("board_template.html").read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise RuntimeError("the board template must contain the data placeholder exactly once")
    _write_atomic(Path(path), template.replace(PLACEHOLDER, _embed(data)))
