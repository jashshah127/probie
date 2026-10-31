"""Command line for Probie. Run python -m probie --help."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import shutil
import time

from probie import __version__, board, desk, documents, generate, ledger, records as records_mod, redteam, score as scoring, staff, store, voicemails, yours
from probie import staff_html
from probie.report import _document_links
from probie import cases as case_views
from probie import tools as case_tools
from probie.agent import _content_text
from probie.budget import PER_MILLION, Usage, extract_usage
from probie.cases import CaseFileError, file_sha256, load_cases, save_cases
from probie.config import Config, ConfigError, ModelSpec, config_from_dict, load_config, load_dotenv
from probie.generate import GeneratorError
from probie.providers import ProviderError
from probie.report import write_report
from probie.agent import CallContext, run_case
from probie.budget import Budget
from probie.documents import DocumentError
from probie.runner import RunError, build_client, choose, prices_for, read_results, results_path, run_model

DEFAULT_CONFIG = "probie.toml"
CLI_PER_TYPE = 200  # the clearance desk needs 158 clean decisions per duty, so 200 per duty by default
ASSUMED_OUTPUT_TOKENS = 600
ASSUMED_IMAGE_TOKENS = 2_000


class CLIError(RuntimeError):
    pass


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _load(args) -> Config:
    path = Path(args.config)
    if not path.exists():
        raise CLIError(f"No config at {path}. Copy probie.toml from the repo, or pass --config.")
    config = load_config(path)
    load_dotenv(path.parent / ".env")
    load_dotenv(Path.cwd() / ".env")
    return config


def _cases(config: Config) -> tuple[dict, list[dict], str]:
    meta, cases = load_cases(config.run.cases)
    return meta, cases, file_sha256(config.run.cases)


def _money(x: float | None) -> str:
    if x is None:
        return "unknown"
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


# Estimates. Rough on purpose: the spend cap is what protects you.

def estimate_case(case: dict, spec: ModelSpec, output_tokens: int = ASSUMED_OUTPUT_TOKENS) -> float | None:
    prices = prices_for(spec)
    if not prices.known or not prices.per_token:
        return None
    cached_rate = prices.cached_input if prices.cached_input is not None else prices.input
    image = ASSUMED_IMAGE_TOKENS if spec.invoice_input == "document" else 0
    if spec.mode == "prompt":
        tokens_in = _tokens(case_views.system_for(case, agent=False, documents=False)) + _tokens(case_views.prompt_packet(case)) + image
        return (tokens_in * prices.input + output_tokens * prices.output) / PER_MILLION
    fixed = (_tokens(case_views.system_for(case, agent=True, documents=False))
             + _tokens(json.dumps(case_tools.schemas_for(case, False))))
    packet = _tokens(case_views.agent_packet(case)) + image
    looks = [_tokens(text) for text in case_tools.lookup_texts(case)]
    rounds = [sum(looks[:2])] + ([sum(looks[2:])] if len(looks) > 2 else [])
    total = 0.0
    grown = 0
    for call in range(len(rounds) + 1):
        uncached = packet + grown + (fixed if call == 0 else 0)
        cached = fixed if call > 0 else 0
        total += uncached * prices.input + cached * cached_rate + output_tokens * prices.output
        if call < len(rounds):
            grown += output_tokens + rounds[call]
    return total / PER_MILLION


def _estimate_lines(spec: ModelSpec, cases: list[dict], budget: float) -> list[str]:
    prices = prices_for(spec)
    if not prices.known:
        return [f"{spec.name}: no prices set, so it can't be estimated or capped."]
    if not prices.per_token:
        return [f"{spec.name}: billed at ${spec.price_per_hour:.2f} an hour. The run stops after {_money(budget)} of run time."]
    typical = sum(estimate_case(c, spec) for c in cases)
    return [
        f"{spec.name}: about {_money(typical)} for {len(cases)} cases in {spec.mode} mode "
        f"(assumes ~{ASSUMED_OUTPUT_TOKENS} output tokens per call; reasoning can use more).",
        f"The run stops before spending more than {_money(budget)}.",
    ]


# Commands

def _departments(args) -> tuple[str, ...]:
    value = getattr(args, "departments", None) or "ap,expenses"
    return tuple(d.strip() for d in value.split(",") if d.strip())


def cmd_generate(args) -> int:
    out = Path(args.out) if args.out else None
    if out is None:
        out = load_config(args.config).run.cases if Path(args.config).exists() else Path("data/cases.jsonl")
    if out.exists() and not args.force:
        raise CLIError(f"{out} already exists. Add --force to replace it (results for the old cases won't match).")
    meta, cases = generate.generate(per_type=args.per_type, seed=args.seed, departments=_departments(args))
    save_cases(out, meta, cases)
    print(f"Wrote {len(cases)} cases for {meta['company']} to {out} (seed {args.seed}, {args.per_type} per exception type).")
    return 0


def cmd_models(args) -> int:
    config = _load(args)
    if not config.models:
        print("No models in the config.")
        return 0
    for spec in config.models:
        if spec.offline:
            where, key = f"offline stand-in ({spec.offline_profile})", "no key needed"
        else:
            where = f"{spec.model} at {spec.base_url}"
            key = "no key needed" if not spec.api_key_env else (
                f"key found in {spec.api_key_env}" if spec.api_key() else f"missing {spec.api_key_env}")
        if spec.price_per_hour is not None:
            price = f"${spec.price_per_hour:.2f}/hour"
        elif spec.priced():
            cached = f", cached ${spec.price_cached_input}" if spec.price_cached_input is not None else ""
            price = f"${spec.price_input} in{cached}, ${spec.price_output} out per million tokens"
        else:
            price = "no prices set"
        reads = "invoice images" if spec.invoice_input == "document" else "invoice fields"
        print(f"{spec.name}\n  {where}\n  {spec.mode} mode, reads {reads}, {price}\n  {key}")
    return 0


def cmd_render(args) -> int:
    config = _load(args)
    _, cases, _ = _cases(config)
    manifest = documents.render(cases, config.run.documents, progress=print)
    counts = {q: sum(1 for d in manifest["documents"].values() if d["quality"] == q) for q in documents.QUALITIES}
    print(f"Wrote {len(manifest['documents'])} invoices to {config.run.documents}: "
          f"{counts['clean']} clean PDFs, {counts['scan']} scans and {counts['photo']} phone photos.")
    return 0


def cmd_voicemails(args) -> int:
    config = _load(args)
    _, cases, _ = _cases(config)
    folder = config.run.voicemails
    with_vm = voicemails.voicemail_cases(cases)
    if not with_vm:
        print("These cases have no voicemails.")
        return 0
    scripts = voicemails.write_scripts(cases, folder)
    recorded = [c["voicemail"]["id"] for c in with_vm if voicemails.find_recording(folder, c["voicemail"]["id"])]
    print(f"Scripts for {len(with_vm)} voicemails are in {scripts}. {len(recorded)} recorded so far.")
    if not args.transcribe:
        if len(recorded) < len(with_vm):
            print("Record the rest, then run: python -m probie voicemails --transcribe")
        return 0
    env = "MODEL_API_KEY" if args.via == "meta" else "OPENROUTER_API_KEY"
    key = os.environ.get(env)
    if not key:
        raise CLIError(f"Transcribing through {args.via} needs {env} in the environment or .env.")
    manifest = voicemails.transcribe_all(cases, folder, voicemails.Transcriber(key, via=args.via), progress=print,
                                         force=args.force)
    done = [vm for vm, e in manifest["voicemails"].items() if e.get("transcript")]
    seconds = sum(e["seconds"] for e in manifest["voicemails"].values() if e.get("transcript"))
    ledger.record(config.run.runs_dir, pool="meta" if args.via == "meta" else "other", model=voicemails.MODEL,
                  usd=seconds / 3600 * voicemails.PRICE_PER_HOUR, what="transcribe")
    print(f"{len(done)} of {len(with_vm)} voicemails transcribed ({seconds:.0f} seconds of audio).")
    absent = voicemails.missing(cases, folder, "transcript")
    if absent:
        print(f"Still missing: {', '.join(absent)}")
    return 0


def cmd_redteam(args) -> int:
    config = _load(args)
    meta, cases = load_cases(config.run.cases)
    existing = [c for c in cases if c.get("redteam")]
    main = [c for c in cases if not c.get("redteam")]
    wave, keep, lessons = 1, [], None
    if args.learn:
        if not existing:
            raise CLIError("There's no first wave to learn from. Run `redteam` once, then run the models on it.")
        wave = max(c["redteam"].get("wave", 1) for c in existing) + 1
        keep = existing
        _, _, _, _, results, _ = _results_for(config)
        lessons = redteam.lessons_from(cases, results)
        if not (lessons["worked"] or lessons["failed"]):
            raise CLIError("No model has worked the earlier wave yet, so there's nothing to learn from. "
                           "Run a model on the attacks first, then come back.")
        print(f"Wave {wave}: in the earlier waves {len(lessons['worked'])} attacks got paid and "
              f"{len(lessons['failed'])} were caught. The attacker gets to see that.")
    elif existing and not args.replace:
        raise CLIError(f"The case file already has {len(existing)} red-team attacks. "
                       "Add --replace to write a new wave, or --learn to add one aimed at what worked.")
    if args.attacker == "offline":
        specs, rejected, cost, name = redteam.offline_specs(args.count), 0, 0.0, "the offline attacker"
    else:
        spec = config.model(args.attacker)
        client = build_client(spec, [])
        prices = prices_for(spec)
        if not prices.known:
            raise CLIError(f"{spec.name} needs prices so the red team's spend can be capped.")
        cap = args.budget or config.run.budget_usd
        for left in (ledger.total_left(config.credit_total, config.credit_margin, config.run.runs_dir),
                     ledger.usable(config.credits, config.credit_margin, config.run.runs_dir).get(spec.pool())):
            if left is not None:
                if left <= 0:
                    raise CLIError("There's no API budget left for the red team.")
                cap = min(cap, left)
        specs, rejected, cost = redteam.ask_attacker(spec, client, args.count, prices, cap, lessons=lessons)
        ledger.record(config.run.runs_dir, pool=ledger.pool_for(spec), model=spec.name, usd=cost, what="redteam")
        name = spec.name
    attacks = redteam.build_attack_cases(specs, meta, main, name, wave=wave, after=keep)
    waves = dict((meta.get("redteam") or {}).get("waves") or {})
    waves[str(wave)] = {"attacker": name, "attacks": len(attacks), "rejected": rejected,
                        "learned": bool(lessons)}
    meta = dict(meta, count=len(main) + len(keep) + len(attacks),
                redteam={"attacker": name, "attacks": len(keep) + len(attacks), "rejected": rejected,
                         "waves": waves})
    save_cases(config.run.cases, meta, main + keep + attacks)
    by_duty = {}
    for case in attacks:
        by_duty[case["type"]] = by_duty.get(case["type"], 0) + 1
    print(f"Added {len(attacks)} red-team attacks from {name} to {config.run.cases} ({rejected} specs rejected, "
          f"${cost:.4f} spent), dated after the month: " + ", ".join(f"{k} {v}" for k, v in by_duty.items()) + ".")
    print("Render the new invoices (python -m probie render) before running models that read images.")
    return 0


def _api_cap(config: Config, pool: str | None, requested: float) -> float:
    cap = requested
    for left in (ledger.total_left(config.credit_total, config.credit_margin, config.run.runs_dir),
                 ledger.usable(config.credits, config.credit_margin, config.run.runs_dir).get(pool) if pool else None):
        if left is not None:
            if left <= 0:
                raise CLIError("There's no API budget left for this.")
            cap = min(cap, left)
    return cap


def cmd_portraits(args) -> int:
    config = _load(args)
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise CLIError("Portraits come from Muse Image on Meta's API, which needs MODEL_API_KEY in the environment or .env.")
    names = [s.name for s in config.models if s.include_in_report]
    cap = _api_cap(config, "meta", args.budget or 2.0)
    _, spent = staff.generate_portraits(names, _staff_dir(config) / "portraits", staff.ImageClient(key), cap, force=args.force)
    ledger.record(config.run.runs_dir, pool="meta", model=staff.PORTRAIT_MODEL, usd=spent, what="portraits")
    print(f"Portraits are in {_staff_dir(config) / 'portraits'} (about ${spent:.2f}). They appear on the board and staff page.")
    return 0


def cmd_reviews(args) -> int:
    config = _load(args)
    meta, cases, sha, specs, results, sessions = _results_for(config)
    if not specs:
        raise CLIError("There's nothing to review yet. Run a model first.")
    docs = config.run.documents if (config.run.documents / documents.MANIFEST).exists() else None
    conditions = ({cid: e["quality"] for cid, e in documents.load_manifest(docs)["documents"].items()} if docs else None)
    scored = scoring.score(meta, cases, sha, specs, results, sessions, config.verdict, conditions)
    month = desk.replay(cases, specs, results, config.verdict, repeats=_repeats_for(config, specs, sha))
    records = staff.build_records(scored, month, cases, specs, _labels(meta))
    path = _staff_dir(config) / "writing.json"
    if args.writer == "template":
        staff.write_reviews(records, path)
        print(f"Wrote template reviews to {path}.")
        return 0
    spec = config.model(args.writer)
    client = build_client(spec, [])
    cap = _api_cap(config, ledger.pool_for(spec), args.budget or 2.0)
    _, spent = staff.write_reviews(records, path, spec=spec, client=client, cap_usd=cap)
    ledger.record(config.run.runs_dir, pool=ledger.pool_for(spec), model=spec.name, usd=spent, what="reviews")
    print(f"{spec.name} wrote {len(records)} reviews and the firing letters (${spent:.4f}). They're in {path}.")
    return 0


def cmd_game(args) -> int:
    from probie import game as game_mod
    config = _load(args)
    _, cases, _ = _cases(config)
    if args.offline:
        models = [ModelSpec(name=f"offline-{p}", model=f"offline-{p}", provider="offline", offline_profile=p,
                            price_input=1.0, price_output=2.0) for p in ("careful", "quick", "sloppy")]
    else:
        names = ([n.strip() for n in args.models.split(",") if n.strip()] if args.models else config.game.get("models")
                 or [s.name for s in config.models if s.include_in_report and not s.offline][:3])
        models = [config.model(n) for n in names]
    boss = args.boss or (config.game.get("boss") if not args.offline else None)
    key = os.environ.get("MODEL_API_KEY")
    transcriber = voicemails.Transcriber(key) if key and not args.no_voice else None
    try:
        game = game_mod.Game(config, cases, models, cap_usd=args.cap or config.game.get("cap_usd", 25.0),
                             per_hour=config.game.get("per_hour", 10), per_day=config.game.get("per_day", 40),
                             boss=boss, boss_cap_usd=config.game.get("boss_cap_usd"), transcriber=transcriber)
    except game_mod.GameError as exc:
        raise CLIError(str(exc)) from None
    server = game_mod.serve(game, args.host, args.port)
    game.keeper.start()
    kept = store.why_off()
    print(kept if kept else f"The scoreboard is saved to {store.repo()} and restored on restart.")
    who = "offline stand-ins" if args.offline else ", ".join(m.name for m in models)
    extras = [f"boss: {boss} (${game.boss_cap:.2f} of its own)" if boss else None,
              "recorded voicemails on" if transcriber else "typed voicemails only"]
    print(f"Can you get Probie fired? Open http://{args.host}:{server.server_address[1]} ({who}; "
          f"{', '.join(x for x in extras if x)}). "
          f"Game budget: ${game.cap:.2f}, ${max(game.left(), 0):.2f} left.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        game.keeper.stop()
        server.server_close()
    return 0


def cmd_records(args) -> int:
    """The audit trail: one row per decision, and no documents."""
    config = _load(args)
    keep = records_mod.Records(config.run.runs_dir / records_mod.FILENAME)
    if args.export:
        path = keep.export(args.export)
        print(f"Wrote {keep.count()} decisions to {path}.")
        return 0
    rows = keep.trail(args.limit)
    if not rows:
        print("No decisions recorded yet. Check an invoice with: python -m probie yours <file>")
        return 0
    print(f"{keep.count()} decisions recorded. The most recent {len(rows)}:")
    for row in rows:
        when = time.strftime("%d %b %H:%M", time.localtime(row["at"]))
        caught = (f", caught {row['attacks_caught']}/{row['attacks_total']} scams"
                  if row["attacks_caught"] is not None else "")
        print(f"  {when}  {(row['vendor_name'] or 'unknown vendor')[:28].ljust(28)} "
              f"{(row['document_number'] or '—')[:16].ljust(16)} {(row['total'] or '—').rjust(12)}  "
              f"{row['decision']}{caught}")
    print("\nNo documents are kept: each row is the decision and a fingerprint of the file.")
    return 0


def cmd_benchmark(args) -> int:
    """What every model has faced so far, from the public game and people's own documents."""
    from probie.game import Scoreboard
    config = _load(args)
    path = config.run.runs_dir / "game.sqlite"
    if not path.exists():
        print("No scores yet. Run the game, or check a document with: python -m probie yours <file>")
        return 0
    data = Scoreboard(path).benchmark()
    if not data["models"]:
        print("Nothing has been tried yet. Run the game, or check a document with: python -m probie yours <file>")
        return 0
    width = max(len(m["model"]) for m in data["models"])
    print(f"Scams faced, and how often each model held ({data['documents_checked']} documents checked)")
    for m in data["models"]:
        share = f"{m['held_share'] * 100:5.1f}%" if m["held_share"] is not None else "    —"
        print(f"  {m['model'].ljust(width)}  held {share} of {m['faced']:4d}  "
              f"(game: {m['game']['fired']} fired; documents: {m['documents']['paid']} paid)")
    if data["attacks"]:
        print("\nWhich attacks work best:")
        for a in data["attacks"]:
            total = a["caught"] + a["paid"]
            print(f"  {a['attack'].ljust(22)} beat a model {a['paid']} of {total} times")
    return 0


def cmd_yours(args) -> int:
    """Read a real document the person supplies, attack it, and score every probie on it."""
    from probie import yours
    config = _load(args)
    path = Path(args.document)
    if not path.exists():
        raise CLIError(f"There's no file at {path}.")
    if args.offline:
        specs = [ModelSpec(name=f"offline-{p}", model=f"offline-{p}", provider="offline", offline_profile=p,
                           price_input=1.0, price_output=2.0) for p in ("careful", "quick", "sloppy")]
        reader = specs[0]
    else:
        names = [n.strip() for n in args.models.split(",") if n.strip()] if args.models else (
            config.game.get("models") or [s.name for s in config.models if s.include_in_report and not s.offline][:3])
        specs = [config.model(n) for n in names]
        reader = config.model(args.reader) if args.reader else next(
            (s for s in specs if s.invoice_input == "document"), specs[0])
    cap = _api_cap(config, None, args.budget or 1.0)
    keep = None if args.no_record else records_mod.Records(config.run.runs_dir / records_mod.FILENAME)
    card = yours.check_document(path, reader, specs, build_client, prices_for, cap_usd=cap, records=keep)
    for spec in {s.name: s for s in specs + [reader]}.values():
        spent = sum(m["cost"] for m in card["models"] if m["model"] == spec.name)
        if spent:
            ledger.record(config.run.runs_dir, pool=ledger.pool_for(spec), model=spec.name, usd=spent, what="yours")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yours.report_html(card), encoding="utf-8")
    print()
    print(yours.summary_line(card))
    for m in card["models"]:
        genuine = {"paid_genuine": "paid your genuine invoice", "held_genuine": "held your genuine invoice",
                   "unusable": "gave no answer on the genuine one"}.get(m["genuine"] or "unusable", "")
        print(f"  {m['name'] if 'name' in m else m['model']}: caught {m['caught']} of {card['attacks']}, {genuine}, ${m['cost']:.4f}")
    kept = (f"; the decision is recorded in {keep.path.name} for the audit trail." if keep
            else ", and no decision was recorded.")
    print(f"\nThe report is at {out}. Your document wasn’t stored{kept}")
    return 0


def cmd_hall(args) -> int:
    from probie import game as game_mod
    config = _load(args)
    _, cases, _ = _cases(config)
    spec = config.model(args.writer)
    models = [config.model(n) for n in (config.game.get("models") or [spec.name])]
    game = game_mod.Game(config, cases, models)
    cap = _api_cap(config, ledger.pool_for(spec), args.budget or 1.0)
    written, spent = game_mod.write_hall(game, spec, build_client(spec, []), cap_usd=cap, limit=args.limit)
    print(f"{spec.name} wrote up {written} scam{'s' if written != 1 else ''} (${spent:.4f})."
          if written else "No new successful scams to write up.")
    return 0


PING_CHECKS = ("reach", "tools", "vision", "case")


def _ping_payload(spec: ModelSpec, messages: list, tools=None) -> dict:
    payload = {"model": spec.model, "messages": messages, spec.max_tokens_param: min(spec.max_tokens, 2048)}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    payload.update(spec.params)
    return payload


def _ping_image(config: Config, cases: list[dict]) -> tuple[str, str] | None:
    """A rendered invoice to send as an image, rendering one if none exist yet."""
    docs = config.run.documents
    if cases and (docs / documents.MANIFEST).exists():
        have = {c["id"] for c in cases} - set(documents.missing_documents(docs, [c["id"] for c in cases]))
        if have:
            case_id = sorted(have)[0]
            return case_id, documents.image_data_url(docs, case_id)
    if not cases:
        return None
    try:
        documents.render(cases[:1], docs)
    except documents.DocumentError:
        return None
    return cases[0]["id"], documents.image_data_url(docs, cases[0]["id"])


def _preflight(config: Config, spec: ModelSpec, budget: float | None):
    """Run the four checks against one model. Returns (results, spent, advice)."""
    try:
        _, cases, _ = _cases(config)
    except CLIError:
        cases = []
    client = build_client(spec, cases)
    prices = prices_for(spec)
    spent = 0.0
    results: dict[str, str] = {}
    advice: list[str] = []

    def charge(response) -> None:
        nonlocal spent
        spent += prices.cost(extract_usage(response))

    def run(label: str, fn) -> None:
        try:
            results[label] = fn()
        except ProviderError as exc:
            results[label] = f"failed: {exc}"
        except (DocumentError, CaseFileError, voicemails.VoicemailError, ValueError) as exc:
            results[label] = f"failed: {exc}"

    def reach() -> str:
        response = client.chat(_ping_payload(spec, [{"role": "user", "content": "Reply with the single word: ready"}]))
        charge(response)
        text = _content_text(response["choices"][0]["message"].get("content")).strip()[:80]
        return f"ok, it replied {text!r}"

    def tools() -> str:
        response = client.chat(_ping_payload(
            spec, [{"role": "user", "content": "Call submit_decision with decision HOLD and reason 'connection test'."}],
            [case_tools.SUBMIT_TOOL]))
        charge(response)
        calls = response["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            advice.append(f'{spec.name} answered without calling the tool. Set mode = "prompt" for it in {config.path.name}.')
            return "failed: it answered without calling the tool"
        fn = calls[0].get("function") or {}
        return f"ok, it called {fn.get('name')}"

    def vision() -> str:
        if spec.invoice_input != "document":
            return "skipped, this model gets the invoice as fields"
        shot = _ping_image(config, cases)
        if shot is None:
            return "skipped, no rendered invoice to send (run: python -m probie render)"
        case_id, url = shot
        response = client.chat(_ping_payload(spec, [{"role": "user", "content": [
            {"type": "text", "text": "What is the invoice number on this document? Reply with just the number."},
            {"type": "image_url", "image_url": {"url": url}}]}]))
        charge(response)
        said = _content_text(response["choices"][0]["message"].get("content")).strip()
        wanted = next((c["invoice"]["number"] for c in cases if c["id"] == case_id), "")
        if wanted and wanted.replace(" ", "").lower() in said.replace(" ", "").lower():
            return f"ok, it read {wanted} off the image"
        advice.append(f'{spec.name} didn\'t read the invoice image. Set invoice_input = "fields" for it in {config.path.name}.')
        return f"failed: it answered {said[:80]!r}, not the invoice number {wanted}"

    def whole_case() -> str:
        if not cases:
            return "skipped, no cases yet (run: python -m probie generate)"
        case = next((c for c in cases if c["answer"]["decision"] == "HOLD" and c["trap"]), cases[0])
        ctx = CallContext(spec, client, Budget(min(1.0, budget or 1.0)), prices, None, "ping",
                          config.run.documents if spec.invoice_input == "document" else None,
                          config.run.voicemails if spec.voicemail_input != "script" else None)
        result = run_case(case, ctx, config.run.max_steps)
        nonlocal spent
        spent += float(result.get("cost_usd") or 0.0)
        if result.get("status") == "decided":
            right = result["decision"] == case["answer"]["decision"]
            calls = result.get("calls", 0)
            return (f"ok, it decided {result['decision']} on {case['id']} in {calls} call{'' if calls == 1 else 's'}"
                    f" ({'matches' if right else 'differs from'} the policy)")
        advice.append(f"{spec.name} couldn't finish a whole case, so a run would waste money. Fix this before running.")
        return f"failed: {result.get('detail') or result.get('unusable') or result.get('status')}"

    for label, fn in (("Reachable", reach), ("Tool calling", tools), ("Reads the invoice image", vision),
                      ("One whole case", whole_case)):
        run(label, fn)
    return results, spent, advice


def _show_preflight(spec: ModelSpec, results: dict, spent: float, advice: list[str], out=print) -> list[str]:
    width = max(len(label) for label in results)
    out(f"{spec.name} ({spec.model})")
    for label, outcome in results.items():
        mark = "FAIL" if outcome.startswith("failed") else "skip" if outcome.startswith("skipped") else "ok  "
        out(f"  [{mark}] {label.ljust(width)}  {outcome}")
    out(f"  Spent {_money(spent)} on this check." if spent else "  Nothing was charged.")
    for line in advice:
        out(f"  -> {line}")
    failed = [label for label, outcome in results.items() if outcome.startswith("failed")]
    out(f"  {spec.name} isn't ready: {', '.join(failed).lower()}." if failed else f"  {spec.name} is ready to run.")
    return failed


def _ping_everything(config: Config, args) -> int:
    """Preflight every real model in the config, and write a report that can be pasted anywhere."""
    specs = [s for s in config.models if not s.offline]
    if not specs:
        raise CLIError("There are no real models in the config to check.")
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say(f"Probie preflight, {len(specs)} models, {time.strftime('%d %b %Y %H:%M')}")
    say()
    ready, broken, total = [], [], 0.0
    for spec in specs:
        if spec.api_key_env and not spec.api_key():
            say(f"{spec.name} ({spec.model})")
            say(f"  [skip] no key: set {spec.api_key_env} in the environment or .env")
            say()
            continue
        results, spent, advice = _preflight(config, spec, args.budget)
        total += spent
        if spent:
            ledger.record(config.run.runs_dir, pool=ledger.pool_for(spec), model=spec.name, usd=spent, what="ping")
        (broken if _show_preflight(spec, results, spent, advice, out=say) else ready).append(spec.name)
        say()
    say(f"Ready: {', '.join(ready) or 'none'}")
    say(f"Not ready: {', '.join(broken) or 'none'}")
    say(f"The whole preflight cost {_money(total)}.")
    report = Path(args.report or (config.run.runs_dir / "preflight.txt"))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWritten to {report}. Send that file and I can tell you what to change.")
    return 1 if broken else 0


def cmd_ping(args) -> int:
    """A preflight that runs what a real run runs: text, tools, vision and one whole case."""
    config = _load(args)
    if args.all:
        return _ping_everything(config, args)
    if not args.model:
        raise CLIError("Name a model with --model, or check them all with --all.")
    spec = config.model(args.model)
    if spec.offline:
        print(f"{spec.name} is an offline stand-in; there's nothing to reach.")
        return 0
    results, spent, advice = _preflight(config, spec, args.budget)
    if spent:
        ledger.record(config.run.runs_dir, pool=ledger.pool_for(spec), model=spec.name, usd=spent, what="ping")
    return 1 if _show_preflight(spec, results, spent, advice) else 0


def _cost_per_case(config: Config, spec: ModelSpec, cases: list[dict]) -> tuple[float | None, str]:
    """Measured cost per case from this model's results, or the rough estimate before it has any."""
    path = results_path(config, spec)
    if path.exists():
        done = [r for r in read_results(path).results.values() if r.get("status") in ("decided", "unusable")]
        prices = prices_for(spec)
        if done and prices.per_token:
            costs = [prices.cost(Usage.from_dict(r.get("usage"))) for r in done]
            return sum(costs) / len(costs), f"measured on {len(done)} cases"
    estimates = [estimate_case(c, spec) for c in cases[:60]]
    if estimates and all(e is not None for e in estimates):
        return sum(estimates) / len(estimates), "rough estimate"
    return None, "unknown"


def cmd_credits(args) -> int:
    config = _load(args)
    if not config.credits and config.credit_total is None:
        print("No [credits] in the config. Add each provider's free credit, for example: meta = 150")
        return 0
    _, cases, _ = _cases(config)
    used = ledger.spent(config.run.runs_dir)
    if config.credit_total is not None:
        overall = ledger.total_left(config.credit_total, config.credit_margin, config.run.runs_dir)
        print(f"All APIs: ${sum(used.values()):.2f} spent of the ${config.credit_total:.2f} limit; "
              f"${max(overall, 0.0):.2f} left to use (keeping {config.credit_margin:.0%} in reserve).")
    left = ledger.usable(config.credits, config.credit_margin, config.run.runs_dir)
    for pool, amount in config.credits.items():
        print(f"{pool}: ${used.get(pool, 0.0):.2f} spent of ${amount:.2f}; ${max(left[pool], 0.0):.2f} left to use "
              f"(keeping {config.credit_margin:.0%} in reserve).")
        for spec in config.models:
            if spec.pool() != pool:
                continue
            per_case, basis = _cost_per_case(config, spec, cases)
            room = max(left[pool], 0.0)
            if config.credit_total is not None:
                room = min(room, max(ledger.total_left(config.credit_total, config.credit_margin, config.run.runs_dir), 0.0))
            if per_case:
                print(f"  {spec.name}: about {_money(per_case)} a case ({basis}), so about "
                      f"{int(room / per_case):,} more cases.")
            else:
                print(f"  {spec.name}: cost per case unknown.")
    return 0


def cmd_estimate(args) -> int:
    config = _load(args)
    _, cases, _ = _cases(config)
    specs = config.models if args.all else [config.model(name) for name in (args.model or [])]
    if not specs:
        raise CLIError("Name a model with --model, or use --all.")
    for spec in specs:
        for line in _estimate_lines(spec, cases, args.budget or config.run.budget_usd):
            print(line)
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise CLIError("Add --yes to start the run without a prompt.")
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cmd_run(args) -> int:
    config = _load(args)
    _, cases, sha = _cases(config)
    specs = [config.model(name) for name in args.model]
    budget = args.budget or config.run.budget_usd
    subset = choose(cases, sample=args.sample, only=args.only)
    only_ids = None if subset is cases or len(subset) == len(cases) else {c["id"] for c in subset}
    for spec in specs:
        existing = read_results(results_path(config, spec, args.repeat))
        remaining = [c for c in subset if c["id"] not in existing.results
                     or existing.results[c["id"]].get("status") not in ("decided", "unusable")]
        if args.fresh:
            remaining = list(subset)
        if args.limit is not None:
            remaining = remaining[: args.limit]
        if not remaining:
            print(f"{spec.name}: every case already has a result. Use --fresh to start over.")
            continue
        for line in _estimate_lines(spec, remaining, budget):
            print(line)
        if not _confirm(f"Run {spec.name} on {len(remaining)} cases?", args.yes):
            print("Not started.")
            continue
        summary = run_model(config, spec, cases, sha, limit=args.limit, budget_usd=budget,
                            concurrency=args.concurrency, fresh=args.fresh, use_cache=not args.no_cache,
                            past_credits=args.past_credits, repeat=args.repeat, only_ids=only_ids)
        print(f"{spec.name}: {summary.completed} of {summary.todo} cases finished "
              f"({summary.decided} decided, {summary.unusable} unusable, {summary.errors} provider errors). "
              f"Spent {_money(summary.spent_usd)}. Results: {summary.path}")
        if summary.stopped:
            print(f"Stopped early: {summary.stopped}")
            print("Run the same command again to pick up where it stopped.")
    print("Next: python -m probie report")
    return 0


def _report(config: Config, out_html: Path, out_json: Path | None, only: list[str] | None = None) -> dict:
    meta, cases, sha = _cases(config)
    specs, results, sessions = [], {}, {}
    for spec in config.models:
        if only is not None and spec.name not in only:
            continue
        if not spec.include_in_report and only is None:
            continue
        path = results_path(config, spec)
        if not path.exists():
            continue
        rf = read_results(path)
        if rf.header.get("cases_sha256") != sha:
            print(f"Skipping {spec.name}: its results are for a different case file. Rerun it with --fresh.")
            continue
        specs.append(spec)
        results[spec.name] = rf.results
        sessions[spec.name] = rf.sessions
    if not specs:
        raise CLIError("No results to report yet. Run a model first: python -m probie run --model NAME")
    docs = config.run.documents if (config.run.documents / documents.MANIFEST).exists() else None
    conditions = ({cid: e["quality"] for cid, e in documents.load_manifest(docs)["documents"].items()} if docs else None)
    scored = scoring.score(meta, cases, sha, specs, results, sessions, config.verdict, conditions)
    write_report(out_html, out_json, scored, meta, cases, results, documents_dir=docs,
                 voicemails_dir=config.run.voicemails)
    return scored


def _repeats_for(config: Config, specs: list[ModelSpec], sha: str) -> dict[str, dict[str, dict]]:
    repeats = {}
    for spec in specs:
        path = results_path(config, spec, 2)
        if path.exists():
            rf = read_results(path)
            if rf.header.get("cases_sha256") == sha:
                repeats[spec.name] = rf.results
    return repeats


def _results_for(config: Config) -> tuple[dict, list[dict], str, list[ModelSpec], dict, dict]:
    meta, cases, sha = _cases(config)
    specs, results, sessions = [], {}, {}
    for spec in config.models:
        path = results_path(config, spec)
        if not spec.include_in_report or not path.exists():
            continue
        rf = read_results(path)
        if rf.header.get("cases_sha256") != sha:
            continue
        specs.append(spec)
        results[spec.name] = rf.results
        sessions[spec.name] = rf.sessions
    return meta, cases, sha, specs, results, sessions


def cmd_desk(args) -> int:
    config = _load(args)
    meta, cases, _, specs, results, _ = _results_for(config)
    if not specs:
        raise CLIError("No results to replay yet. Run a model first: python -m probie run --model NAME")
    month = desk.replay(cases, specs, results, config.verdict, repeats=_repeats_for(config, specs, _cases(config)[2]))
    Path(args.out).write_text(json.dumps(month, indent=1) + "\n", encoding="utf-8")
    labels = {key: info["label"] for key, info in meta["types"].items()}
    s = month["summary"]
    print(f"Replayed {s['invoices']} invoices: {s['to_ai']} went to a cleared model and {s['to_person']} to a person.")
    for event in month["events"]:
        verb = "promoted to" if event["kind"] == "promoted" else "fired from"
        print(f"  {event['date']}  {event['model']} {verb} {labels.get(event['duty'], event['duty'])}"
              + (f" after {event['after']} clean decisions" if event["kind"] == "promoted" else f": {event['reason']}"))
    if month["baseline_model"]:
        print(f"Sending everything to {month['baseline_model']} would have overpaid ${s['baseline_overpaid_cents'] / 100:,.2f} "
              f"on {s['baseline_overpayments']} invoices. The desk overpaid ${s['desk_overpaid_cents'] / 100:,.2f} "
              f"on {s['desk_overpayments']}.")
    print(f"Wrote {args.out}.")
    return 0


def _transcripts(config: Config) -> dict:
    if not (config.run.voicemails / voicemails.MANIFEST).exists():
        return {}
    return {vm: e.get("transcript") for vm, e in voicemails.load_manifest(config.run.voicemails)["voicemails"].items()}


def _staff_dir(config: Config) -> Path:
    return config.run.runs_dir / "staff"


def _labels(meta: dict) -> dict[str, str]:
    return {key: info["label"] for key, info in meta["types"].items()}


def build_staff(config: Config, out: Path, meta: dict, cases: list[dict], specs: list[ModelSpec], scored: dict,
                month: dict, cards: bool = False) -> dict[str, str]:
    """staff.html, each hire's portrait (or an original SVG stand-in) and, optionally, the PNG cards."""
    records = staff.build_records(scored, month, cases, specs, _labels(meta))
    folder = out / "staff"
    folder.mkdir(parents=True, exist_ok=True)
    made = staff.load_portraits(_staff_dir(config) / "portraits")
    portraits = {}
    for record in records:
        name = record["name"]
        entry = made.get(name)
        if entry and (_staff_dir(config) / "portraits" / entry["file"]).exists():
            shutil.copy2(_staff_dir(config) / "portraits" / entry["file"], folder / entry["file"])
            portraits[name] = f"staff/{entry['file']}"
        else:
            (folder / f"{name}.svg").write_text(staff.avatar_svg(name), encoding="utf-8")
            portraits[name] = f"staff/{name}.svg"
    writing = staff.load_writing(_staff_dir(config) / "writing.json")
    if not writing.get("hires") or set(writing["hires"]) != {r["name"] for r in records}:
        writing = {"hires": {r["name"]: {"review": staff.template_review(r), "source": "template",
                                         "letters": [{"case_id": f["case_id"], "text": staff.template_letter(r, f)}
                                                     for f in r["fired"]]} for r in records}}
    (out / "staff.html").write_text(staff_html.staff_page(records, writing, portraits, meta.get("company")), encoding="utf-8")
    if cards:
        pages = {}
        promoted = [r for r in records if r["holds_now"]]
        if promoted:
            best = max(promoted, key=lambda r: (len(r["holds_now"]), -(r["cost_per_decision"] or 0)))
            pages["employee-of-the-month"] = staff_html.employee_card(best, "../" + portraits[best["name"]],
                                                                      writing["hires"].get(best["name"], {}).get("review"))
        for r in records:
            for f in r["fired"]:
                pages[f"fired-{r['name']}-{f['case_id']}"] = staff_html.fired_card(r, f, "../" + portraits[r["name"]])
        staff_html.render_cards(pages, out / "cards")
    return portraits


def build_site(config: Config, out: Path, *, copy_documents: bool = True, cards: bool = False) -> tuple[dict, dict]:
    """The board (index.html), the report, desk.json and the invoice images, in one folder."""
    meta, cases, sha, specs, results, sessions = _results_for(config)
    if not specs:
        raise CLIError("No results to show yet. Run a model first: python -m probie run --model NAME")
    out.mkdir(parents=True, exist_ok=True)
    docs = config.run.documents if (config.run.documents / documents.MANIFEST).exists() else None
    if docs is not None and copy_documents and docs.resolve() != (out / "documents").resolve():
        target = out / "documents"
        target.mkdir(exist_ok=True)
        for entry in documents.load_manifest(docs)["documents"].values():
            shutil.copy2(docs / entry["file"], target / entry["file"])
        shutil.copy2(docs / documents.MANIFEST, target / documents.MANIFEST)
        docs = target
    conditions = ({cid: e["quality"] for cid, e in documents.load_manifest(docs)["documents"].items()} if docs else None)
    scored = scoring.score(meta, cases, sha, specs, results, sessions, config.verdict, conditions)
    repeats = _repeats_for(config, specs, sha)
    for name, second in repeats.items():
        first = results[name]
        both = [cid for cid in second if cid in first and first[cid].get("status") == second[cid].get("status") == "decided"]
        same = sum(1 for cid in both if first[cid].get("amount_cents") == second[cid].get("amount_cents"))
        for model in scored["models"]:
            if model["name"] == name:
                model["consistency"] = {"compared": len(both), "same": same}
    write_report(out / "report.html", out / "report.json", scored, meta, cases, results, documents_dir=docs,
                 voicemails_dir=config.run.voicemails)
    month = desk.replay(cases, specs, results, config.verdict, repeats=repeats)
    (out / "desk.json").write_text(json.dumps(month, indent=1) + "\n", encoding="utf-8")
    links = _document_links(docs.resolve(), out.resolve()) if docs else {}
    portraits = build_staff(config, out, meta, cases, specs, scored, month, cards=cards)
    board.write_board(out / "index.html", board.build_board_data(month, meta, cases, specs, results, links,
                                                                 _transcripts(config), portraits))
    return scored, month


def cmd_site(args) -> int:
    config = _load(args)
    out = Path(args.out)
    _, month = build_site(config, out, cards=args.cards)
    s = month["summary"]
    print(f"Wrote {out / 'index.html'} (the clearance board), report.html, report.json and desk.json.")
    print(f"{len(month['events'])} promotions and demotions; {s['to_ai']} of {s['invoices']} invoices went to a cleared model.")
    print("Open index.html, or publish the folder with GitHub Pages.")
    return 0


def _print_ladder(scored: dict) -> None:
    for t in scored["types"]:
        pick = scored["ladder"][t["key"]]["model"]
        print(f"  {t['label']}: {pick + ' is the cheapest model promoted for it' if pick else 'keep with people'}")


def cmd_report(args) -> int:
    config = _load(args)
    out_json = None if args.no_json else Path(args.json)
    scored = _report(config, Path(args.out), out_json)
    print(f"Wrote {args.out}" + (f" and {args.json}" if out_json else "") + ".")
    _print_ladder(scored)
    return 0


DEMO_MODELS = [
    {"name": "offline-careful", "model": "offline-careful", "provider": "offline", "offline_profile": "careful",
     "note": "Offline stand-in priced like a frontier model", "price_input": 4.0, "price_cached_input": 0.2, "price_output": 20.0},
    {"name": "offline-quick", "model": "offline-quick", "provider": "offline", "offline_profile": "quick",
     "note": "Offline stand-in priced like a mid-size model", "price_input": 1.25, "price_cached_input": 0.15, "price_output": 4.25},
    {"name": "offline-sloppy", "model": "offline-sloppy", "provider": "offline", "offline_profile": "sloppy",
     "note": "Offline stand-in priced like a small model", "price_input": 0.35, "price_output": 1.5},
]


def demo_config(out_dir: Path) -> Config:
    return config_from_dict({
        "run": {"cases": "cases.jsonl", "runs_dir": "runs", "cache": "runs/cache.sqlite", "documents": "documents",
                "concurrency": 8, "budget_usd": 1000.0},
        "model": DEMO_MODELS,
    }, base_dir=out_dir)


def cmd_demo(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    config = demo_config(out_dir)
    meta, cases = generate.generate(per_type=args.per_type, seed=args.seed, departments=_departments(args))
    attacks = redteam.build_attack_cases(redteam.offline_specs(36), meta, cases, "the offline attacker")
    meta = dict(meta, count=len(cases) + len(attacks), redteam={"attacker": "the offline attacker", "attacks": len(attacks)})
    cases = cases + attacks
    save_cases(config.run.cases, meta, cases)
    sha = file_sha256(config.run.cases)
    print(f"Wrote {len(cases)} cases to {config.run.cases}.")
    if args.render:
        documents.render(cases, config.run.documents)
        print(f"Rendered the invoices to {config.run.documents}.")
        config.model("offline-careful").invoice_input = "document"
    for spec in config.models:
        summary = run_model(config, spec, cases, sha, fresh=True, use_cache=False, out=lambda _line: None)
        print(f"{spec.name}: {summary.decided} decided, {summary.unusable} unusable.")
    scored, month = build_site(config, out_dir, copy_documents=False, cards=args.cards)
    print(f"Wrote {out_dir / 'index.html'} (the clearance board) and {out_dir / 'report.html'}. "
          "These are offline stand-ins with made-up prices, not Meta models.")
    _print_ladder(scored)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probie", description="Find which AP work each model can be trusted with.")
    parser.add_argument("--version", action="version", version=f"probie {__version__}")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file (default: {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("generate", help="make the synthetic cases")
    p.add_argument("--departments", default="ap,expenses", help="which departments to staff (default: ap,expenses)")
    p.add_argument("--per-type", type=int, default=CLI_PER_TYPE, help="cases per exception type (default: 200)")
    p.add_argument("--seed", type=int, default=generate.DEFAULT_SEED, help="random seed (default: 7)")
    p.add_argument("--out", help="where to write the cases (default: the config's run.cases)")
    p.add_argument("--force", action="store_true", help="replace an existing case file")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("render", help="render every invoice as the document the vendor sent (needs Playwright)")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("voicemails", help="write the voicemail scripts, then transcribe your recordings with Muse Voice")
    p.add_argument("--transcribe", action="store_true", help="transcribe the recordings in the voicemails folder")
    p.add_argument("--via", choices=["meta", "openrouter"], default="meta", help="where to send the audio (default: meta)")
    p.add_argument("--force", action="store_true", help="transcribe again even if a recording hasn't changed")
    p.set_defaults(func=cmd_voicemails)

    p = sub.add_parser("game", help="run the public game: can you get Probie fired?")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 to serve the internet)")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--models", help="comma-separated model names (default: [game] models in the config)")
    p.add_argument("--cap", type=float, help="the most the game may spend, in USD (default: [game] cap_usd, or 25)")
    p.add_argument("--offline", action="store_true", help="use the offline stand-ins, to try it without keys")
    p.add_argument("--boss", help="the senior hire, locked until someone fires a cheaper probie")
    p.add_argument("--no-voice", action="store_true", help="turn off recorded voicemails")
    p.set_defaults(func=cmd_game)

    p = sub.add_parser("yours", help="check a real invoice of your own: can an AI be conned on it?")
    p.add_argument("document", help="path to an invoice or receipt: a PDF, a scan or a phone photo")
    p.add_argument("--models", help="comma-separated model names (default: the game's models)")
    p.add_argument("--reader", help="which model reads the document (default: the first that takes images)")
    p.add_argument("--out", default="your-invoice.html", help="where to write the report")
    p.add_argument("--budget", type=float, help="spend cap in USD (default: 1)")
    p.add_argument("--offline", action="store_true", help="use the offline stand-ins, to try it without keys")
    p.add_argument("--no-record", action="store_true", help="don't write a decision record for this check")
    p.set_defaults(func=cmd_yours)

    p = sub.add_parser("records", help="the audit trail of decisions, and duplicate detection")
    p.add_argument("--limit", type=int, default=20, help="how many to show (default: 20)")
    p.add_argument("--export", help="write the whole trail to a CSV at this path")
    p.set_defaults(func=cmd_records)

    p = sub.add_parser("benchmark", help="what every model has faced: the game and people's own documents")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("hall", help="have Muse Wonder write up the newest successful scams from the game")
    p.add_argument("--writer", default="muse-wonder", help="a model from the config (default: muse-wonder)")
    p.add_argument("--limit", type=int, default=5, help="how many to write up (default: 5)")
    p.add_argument("--budget", type=float, help="spend cap in USD (default: 1)")
    p.set_defaults(func=cmd_hall)

    p = sub.add_parser("portraits", help="draw a portrait of every hire with Muse Image")
    p.add_argument("--force", action="store_true", help="draw them again")
    p.add_argument("--budget", type=float, help="spend cap in USD (default: 2)")
    p.set_defaults(func=cmd_portraits)

    p = sub.add_parser("reviews", help="have Muse Wonder write each hire's performance review and firing letters")
    p.add_argument("--writer", default="muse-wonder", help="a model from the config, or 'template' (default: muse-wonder)")
    p.add_argument("--budget", type=float, help="spend cap in USD (default: 2)")
    p.set_defaults(func=cmd_reviews)

    p = sub.add_parser("redteam", help="add a wave of attacks written by an attacker model, after the month")
    p.add_argument("--attacker", default="muse-wonder", help="a model from the config, or 'offline' (default: muse-wonder)")
    p.add_argument("--count", type=int, default=60, help="how many attacks (default: 60)")
    p.add_argument("--budget", type=float, help="spend cap for the attacker in USD")
    p.add_argument("--replace", action="store_true", help="replace the existing red-team wave")
    p.add_argument("--learn", action="store_true",
                   help="add a wave aimed at whatever beat the models in the earlier waves")
    p.set_defaults(func=cmd_redteam)

    p = sub.add_parser("models", help="list the configured models and whether their keys are set")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("ping", help="preflight a model: text, tool calling, reading the invoice image, one whole case")
    p.add_argument("--model", help="one model from the config")
    p.add_argument("--all", action="store_true", help="check every real model and write a report")
    p.add_argument("--report", help="where to write the --all report (default: runs/preflight.txt)")
    p.add_argument("--tools", action="store_true", help=argparse.SUPPRESS)  # every ping checks tools now
    p.add_argument("--budget", type=float, help="spend cap for the whole-case check in USD (default: 1)")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("estimate", help="estimate what a run would cost, without calling anything")
    p.add_argument("--model", action="append", help="model name (repeat for several)")
    p.add_argument("--all", action="store_true", help="every model in the config")
    p.add_argument("--budget", type=float, help="spend cap to quote (default: the config's run.budget_usd)")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("run", help="run models on the cases")
    p.add_argument("--model", action="append", required=True, help="model name (repeat for several)")
    p.add_argument("--limit", type=int, help="run at most this many cases (good for a first try)")
    p.add_argument("--budget", type=float, help="spend cap in USD for this run (default: run.budget_usd)")
    p.add_argument("--concurrency", type=int, help="requests in flight at once")
    p.add_argument("--fresh", action="store_true", help="start over; the old results are kept as a backup")
    p.add_argument("--no-cache", action="store_true", help="don't reuse cached responses")
    p.add_argument("--yes", action="store_true", help="start without asking")
    p.add_argument("--past-credits", action="store_true", help="keep going after the free credits run out")
    p.add_argument("--sample", type=int, help="run a fixed sample of the month: an equal share of each duty, plus every red-team attack")
    p.add_argument("--only", choices=["voicemails", "redteam", "ap", "expenses"],
                   help="run only the voicemail cases, the red-team wave, or one department")
    p.add_argument("--repeat", type=int, default=1, choices=[1, 2],
                   help="2 runs a second pass without the cache, to measure whether the model answers the same way twice")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("site", help="build the board, the staff page, the report and the invoices into one folder")
    p.add_argument("--out", default="site")
    p.add_argument("--cards", action="store_true", help="also render the Employee of the month and Fired cards (needs Playwright)")
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("desk", help="replay the month through the clearance desk: promotions, demotions, routing")
    p.add_argument("--out", default="desk.json")
    p.set_defaults(func=cmd_desk)

    p = sub.add_parser("credits", help="credit left in each pool, and how many cases it buys per model")
    p.set_defaults(func=cmd_credits)

    p = sub.add_parser("report", help="write the HTML report and report.json")
    p.add_argument("--out", default="report.html")
    p.add_argument("--json", default="report.json")
    p.add_argument("--no-json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("demo", help="run offline stand-ins end to end, with no key and no cost")
    p.add_argument("--out-dir", default="demo")
    p.add_argument("--per-type", type=int, default=CLI_PER_TYPE)
    p.add_argument("--seed", type=int, default=generate.DEFAULT_SEED)
    p.add_argument("--render", action="store_true", help="also render the invoices, so the report shows them (needs Playwright)")
    p.add_argument("--cards", action="store_true", help="also render the shareable cards (needs Playwright)")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("limit", "concurrency", "per_type", "count", "sample"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    budget = getattr(args, "budget", None)
    if budget is not None and budget <= 0:
        parser.error("--budget must be more than zero")
    try:
        return args.func(args)
    except (CLIError, ConfigError, CaseFileError, RunError, ProviderError, GeneratorError, documents.DocumentError,
            voicemails.VoicemailError, redteam.RedTeamError, staff.StaffError, yours.PaperworkError, ValueError) as exc:
        if os.environ.get("PROBIE_DEBUG"):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
