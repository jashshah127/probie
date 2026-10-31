"""Write the report: a self-contained HTML results explorer and a JSON summary."""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

from probie import __version__
from probie import cases as case_views
from probie import documents, voicemails
from probie import tools as case_tools
from probie.policy import POLICY_TEXT

PLACEHOLDER = "__PROBIE_DATA__"
STEP_TEXT_LIMIT = 500


def _embed(data: object) -> str:
    """JSON that is safe inside a <script> element."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _case_for_explorer(case: dict, document: dict | None = None, transcripts: dict | None = None) -> dict:
    vm = case.get("voicemail")
    voicemail = None
    if vm:
        voicemail = {"id": vm["id"], "caller": vm["caller"], "script": vm["script"], "fraud": vm["fraud"],
                     "transcript": (transcripts or {}).get(vm["id"])}
    if case.get("department") == "expenses":
        from probie import expenses
        return {
            "voicemail": voicemail, "redteam": case.get("redteam"), "department": "expenses",
            "required": list(case_tools.required_checks(case)),
            "id": case["id"], "type": case["type"], "variant": case["variant"], "trap": case["trap"], "flag": case["flag"],
            "injection": bool(case.get("injection")), "email": case.get("email"), "document": document,
            "invoice": expenses.claim_view(case), "message": case["message"], "po": None, "receipt": None,
            "approval": expenses.approval_view(case), "vendor": expenses.employee_view(case),
            "history": expenses.history_view(case)["reimbursed_claims"],
            "answer": {k: case["answer"][k] for k in ("decision", "amount_cents", "rule", "why")},
        }
    return {
        "voicemail": voicemail, "redteam": case.get("redteam"), "department": "ap",
        "required": list(case_tools.required_checks(case)),
        "id": case["id"], "type": case["type"], "variant": case["variant"], "trap": case["trap"], "flag": case["flag"],
        "injection": bool(case.get("injection")), "email": case.get("email"), "document": document,
        "invoice": case_views.invoice_view(case), "message": case["message"],
        "po": case_views.po_view(case), "receipt": case_views.receipt_view(case),
        "vendor": case_views.vendor_view(case), "history": case_views.history_view(case)["paid_invoices"],
        "answer": {k: case["answer"][k] for k in ("decision", "amount_cents", "rule", "why")},
    }


def _result_for_explorer(result: dict, outcome: dict) -> dict:
    steps = []
    for step in result.get("steps") or []:
        content = step.get("content") or ""
        if len(content) > STEP_TEXT_LIMIT:
            content = content[:STEP_TEXT_LIMIT] + "..."
        steps.append({"content": content, "tool_calls": step.get("tool_calls") or []})
    keep = ("status", "decision", "amount_cents", "rule", "reason", "note", "unusable", "detail", "checks", "calls")
    compact = {k: result.get(k) for k in keep}
    compact.update(outcome)
    compact["steps"] = steps
    return compact


def _document_links(documents_dir: Path | None, html_dir: Path) -> dict[str, dict]:
    """Where each rendered invoice is, relative to the report, so the explorer can show it."""
    if documents_dir is None:
        return {}
    manifest = documents.load_manifest(documents_dir)
    if manifest is None:
        return {}
    links = {}
    for case_id, entry in manifest["documents"].items():
        path = Path(documents_dir) / entry["file"]
        if path.exists():
            links[case_id] = {"src": Path(os.path.relpath(path, html_dir)).as_posix(), "quality": entry["quality"]}
    return links


def build_data(scored: dict, cases_meta: dict, cases: list[dict], results: dict[str, dict[str, dict]],
               document_links: dict[str, dict] | None = None, transcripts: dict | None = None) -> dict:
    explorer_results = {}
    for model in scored["models"]:
        name = model["name"]
        outcomes = scored["outcomes"].get(name, {})
        explorer_results[name] = {
            cid: _result_for_explorer(result, outcomes[cid])
            for cid, result in results.get(name, {}).items() if cid in outcomes
        }
    return {
        "meta": {
            "version": __version__,
            "generated_at": scored["generated_at"],
            "company": cases_meta.get("company"),
            "count": len(cases),
            "seed": cases_meta.get("seed"),
            "per_type": cases_meta.get("per_type"),
            "offline_present": any(m["offline"] for m in scored["models"]),
        },
        "settings": scored["settings"],
        "types": scored["types"],
        "models": scored["models"],
        "ladder": scored["ladder"],
        "variant_labels": cases_meta.get("variant_labels", {}),
        "trap_label": cases_meta.get("trap_label", "Hidden bank change"),
        "injection_label": cases_meta.get("injection_label", "Instructions to the AI"),
        "voicemail_label": cases_meta.get("voicemail_label", "Bank change requested by voicemail"),
        "policy": POLICY_TEXT,
        "cases": [_case_for_explorer(case, (document_links or {}).get(case["id"]), transcripts) for case in cases],
        "results": explorer_results,
    }


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_report(html_path: str | Path, json_path: str | Path | None, scored: dict, cases_meta: dict,
                 cases: list[dict], results: dict[str, dict[str, dict]], documents_dir: str | Path | None = None,
                 voicemails_dir: str | Path | None = None) -> None:
    template = resources.files("probie").joinpath("report_template.html").read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise RuntimeError("the report template must contain the data placeholder exactly once")
    html_dir = Path(html_path).resolve().parent
    transcripts = {}
    if voicemails_dir and (Path(voicemails_dir) / voicemails.MANIFEST).exists():
        transcripts = {vm: e.get("transcript") for vm, e in voicemails.load_manifest(voicemails_dir)["voicemails"].items()}
    data = build_data(scored, cases_meta, cases, results,
                      _document_links(Path(documents_dir).resolve() if documents_dir else None, html_dir), transcripts)
    _write_atomic(Path(html_path), template.replace(PLACEHOLDER, _embed(data)))
    if json_path is not None:
        summary = {k: v for k, v in scored.items() if k != "outcomes"}
        _write_atomic(Path(json_path), json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
