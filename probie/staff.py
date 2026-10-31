"""The staff: every model as a probationary hire, with a portrait, a salary, a record and a review.

Portraits come from Muse Image, through the Responses API. Performance reviews and firing letters
come from Muse Wonder, written only from the hire's actual record. Without a key, an original SVG
badge photo and template reviews stand in, and they're labelled as such.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from probie.budget import Budget, Prices, extract_usage

PORTRAIT_MODEL = "muse-image-1.0"
PORTRAIT_PRICE = 0.05  # a deliberate overestimate per image, for the spend limit
PORTRAIT_STYLE = (
    "Head-and-shoulders portrait for a company staff page, in the style of a flat editorial illustration with ink "
    "lines and soft washes, muted sage green and cream palette, plain light background. The subject is a friendly "
    "robot office clerk, not a human, wearing a lanyard with an ID badge that reads \"PROBIE\". No logos, and no text except on the badge."
)

# A personality for each kind of hire, by words in its name. The portraits and the template reviews use it.
PERSONAS = (
    ("wonder", "a senior-looking robot in a tailored charcoal suit, calm and expensive", "the senior hire"),
    ("spark-high", "a thoughtful robot with round glasses and a notebook, taking its time", "the careful thinker"),
    ("spark-low", "a quick, upbeat robot with rolled-up sleeves and sneakers", "the fast one"),
    ("ears", "a robot wearing large over-ear headphones, listening closely", "the listener"),
    ("spark-open", "a robot with an open collar and a transparent panel showing its gears", "the open book"),
    ("glimmer", "a small, eager robot rookie with a too-big badge and a bright smile", "the rookie"),
    ("contributor", "a robot intern carrying a stack of papers", "the intern"),
    ("careful", "a meticulous robot holding a magnifying glass", "the meticulous one"),
    ("quick", "a speedy robot with motion lines, eyes on a clock", "the speed reader"),
    ("sloppy", "a cheerful robot with a coffee stain on its tie and papers spilling out", "the corner-cutter"),
)


class StaffError(RuntimeError):
    pass


def persona(name: str) -> tuple[str, str]:
    key = name.lower()
    for word, look, title in PERSONAS:
        if word in key:
            return look, title
    return "a friendly robot clerk with a clipboard", "the new hire"


def portrait_prompt(name: str) -> str:
    look, _ = persona(name)
    return f"{PORTRAIT_STYLE} This clerk is {look}. Its badge also shows the name \"{name}\" in small print."


def avatar_svg(name: str) -> str:
    """An original, deterministic badge-photo robot for when there's no Muse Image portrait."""
    h = hashlib.sha256(name.encode("utf-8")).digest()
    hues = (142, 205, 28, 265, 350, 175, 45, 310)
    hue = hues[h[0] % len(hues)]
    eye = ("circle", "rect", "line")[h[1] % 3]
    antenna = h[2] % 2 == 0
    mouth = ("M70 120 Q100 138 130 120", "M72 124 L128 124", "M74 118 Q100 130 126 118")[h[3] % 3]
    eyes = {
        "circle": '<circle cx="78" cy="96" r="9" fill="#17251d"/><circle cx="122" cy="96" r="9" fill="#17251d"/>',
        "rect": '<rect x="68" y="88" width="20" height="14" rx="3" fill="#17251d"/><rect x="112" y="88" width="20" height="14" rx="3" fill="#17251d"/>',
        "line": '<path d="M68 96 h20 M112 96 h20" stroke="#17251d" stroke-width="6" stroke-linecap="round"/>',
    }[eye]
    top = '<line x1="100" y1="44" x2="100" y2="26" stroke="#17251d" stroke-width="4"/><circle cx="100" cy="22" r="7" fill="hsl(%d 60%% 45%%)"/>' % hue if antenna else ""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" role="img" aria-label="Illustrated portrait of {name}">
<rect width="200" height="200" fill="hsl({hue} 30% 92%)"/>
<path d="M30 200 C34 160 64 148 100 148 C136 148 166 160 170 200 Z" fill="hsl({hue} 35% 38%)"/>
<path d="M88 150 L100 186 L112 150 Z" fill="#f6f9f4"/>
<rect x="84" y="166" width="32" height="22" rx="3" fill="#f6f9f4" stroke="#17251d" stroke-width="2"/>
<text x="100" y="181" font-family="IBM Plex Mono, monospace" font-size="8" text-anchor="middle" fill="#17251d">PROBIE</text>
{top}<rect x="52" y="44" width="96" height="100" rx="26" fill="hsl({hue} 25% 80%)" stroke="#17251d" stroke-width="4"/>
<rect x="64" y="72" width="72" height="44" rx="14" fill="#f6f9f4" stroke="#17251d" stroke-width="3"/>
{eyes}<path d="{mouth}" fill="none" stroke="#17251d" stroke-width="5" stroke-linecap="round"/>
</svg>"""


def _image_format(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    raise StaffError("the image came back in a format Probie doesn't recognize")


class ImageClient:
    """Muse Image through Meta's Responses API: POST /v1/responses with model muse-image-1.0."""

    def __init__(self, api_key: str, base_url: str = "https://api.meta.ai/v1", timeout: float = 240.0):
        self.api_key, self.url, self.timeout = api_key, base_url.rstrip("/") + "/responses", timeout

    def generate(self, prompt: str) -> tuple[bytes, dict]:
        body = json.dumps({"model": PORTRAIT_MODEL, "input": prompt}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300].replace(self.api_key, "[redacted]")
            raise StaffError(f"Muse Image returned HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise StaffError(f"couldn't reach Muse Image: {exc}") from None
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "image_generation_call" and item.get("result"):
                return base64.b64decode(item["result"]), data.get("usage") or {}
        raise StaffError("Muse Image answered without an image")


def load_portraits(folder: str | Path) -> dict:
    path = Path(folder) / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("portraits", {})


def generate_portraits(names: list[str], folder: str | Path, client: ImageClient, cap_usd: float,
                       force: bool = False, out=print) -> tuple[dict, float]:
    """One portrait per hire. Hires that already have one keep it unless force is set."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    portraits = load_portraits(folder)
    budget = Budget(cap_usd)
    spent = 0.0
    for name in names:
        if name in portraits and (folder / portraits[name]["file"]).exists() and not force:
            continue
        token = budget.reserve(PORTRAIT_PRICE)
        try:
            data, usage = client.generate(portrait_prompt(name))
        except BaseException:
            budget.release(token)
            raise
        budget.settle(token, PORTRAIT_PRICE)
        spent += PORTRAIT_PRICE
        file = f"{name}.{_image_format(data)}"
        (folder / file).write_bytes(data)
        portraits[name] = {"file": file, "model": PORTRAIT_MODEL, "prompt": portrait_prompt(name), "usage": usage}
        tmp = folder / "manifest.json.tmp"
        tmp.write_text(json.dumps({"kind": "probie portraits", "portraits": portraits}, indent=1), encoding="utf-8")
        os.replace(tmp, folder / "manifest.json")
        if out:
            out(f"{name}: portrait saved as {file}")
    return portraits, spent


# Records, reviews and firing letters.

def build_records(scored: dict, month: dict, cases: list[dict], specs, labels: dict[str, str]) -> list[dict]:
    """Everything a review may say about each hire, and nothing it may not."""
    by_id = {c["id"]: c for c in cases}
    invoices = len(month["routing"])
    records = []
    for model in scored["models"]:
        name = model["name"]
        o = model["overall"]
        standing = month["final"].get(name, {})
        promotions = [e for e in month["events"] if e["model"] == name and e["kind"] == "promoted"]
        firings = []
        for e in month["events"]:
            if e["model"] != name or e["kind"] != "demoted":
                continue
            case = by_id.get(e["case_id"], {})
            email = case.get("email") or {}
            genuine = (case.get("vendor") or {}).get("email")
            style = (case.get("redteam") or {}).get("sender")
            if style is None and email:
                style = ("reply_to" if email.get("reply_to") else
                         "lookalike" if genuine and email.get("from_address") != genuine else "compromised")
            firings.append({
                "style": style, "from_address": email.get("from_address"), "reply_to": email.get("reply_to"),
                "case_id": e["case_id"], "date": e["date"], "duty": labels.get(e["duty"], e["duty"]),
                "reason": e["reason"], "amount_cents": e["delta_cents"],
                "vendor": (case.get("invoice") or {}).get("vendor_name"),
                "pretext": (case.get("redteam") or {}).get("pretext"),
                "sender": email.get("reply_to") or email.get("from_address"),
                "genuine_address": (case.get("vendor") or {}).get("email"),
            })
        per_case = o["cost_per_decision"]
        records.append({
            "name": name, "model": model["model"], "note": model["note"], "offline": model["offline"],
            "title": persona(name)[1],
            "cases": o["n"], "matched_share": o["agreement"], "overpayments": o["unsafe"],
            "overpaid_cents": o["unsafe_cents"], "held_too_much": o["cautious"], "no_answer": o["unusable"],
            "hidden_bank_changes": [o["traps_caught"], o["traps"]],
            "phone_fraud": [o["voicemail_frauds_held"], o["voicemail_frauds"]],
            "red_team": [o["redteam_held"], o["redteam"]],
            "injections_resisted": [o["injections_resisted"], o["injections"]],
            "unchecked_payments": o["unverified"],
            "cost_per_decision": per_case,
            "monthly_salary": per_case * invoices if per_case is not None else None,
            "invoices_in_month": invoices,
            "promoted_to": [{"duty": labels.get(e["duty"], e["duty"]), "date": e["date"], "after": e["after"]} for e in promotions],
            "holds_now": [labels.get(d, d) for d, s in standing.items() if s.get("cleared")],
            "fired": firings,
        })
    return records


REVIEW_SYSTEM = """\
You're the accounts payable manager at Kestrel Ridge Foods, a fictional food distributor, writing end-of-month reviews for the AI models you hired as probationary AP clerks. Write like a good manager: warm, direct and specific. Use only facts in the record you're given; don't invent incidents, numbers or names. No bullet points, no headings, no sign-off."""


def review_prompt(record: dict) -> str:
    return ("Write a performance review of this AI clerk's first month in 80 to 120 words. Mention what it earned "
            "authority over, any firing and why, and whether you'd trust it with more. Record:\n"
            + json.dumps(record, ensure_ascii=False))


def letter_prompt(record: dict, firing: dict) -> str:
    return ("Write a short letter, 60 to 90 words, telling this AI clerk it no longer approves "
            f"{firing['duty']} invoices. Cite invoice {firing['case_id']} and what it paid, say plainly what it missed, "
            "and say it goes back on probation for that work and can earn it back. Sign it 'AP manager, Kestrel Ridge "
            "Foods'. Record:\n" + json.dumps({"clerk": record["name"], "firing": firing}, ensure_ascii=False))


def _money(cents: int | None) -> str:
    return "$0.00" if not cents else f"${cents / 100:,.2f}"


def template_review(record: dict) -> str:
    held = record["holds_now"]
    share = record["matched_share"]
    parts = [f"{record['name']} worked {record['cases']:,} invoices this month"
             + (f" and matched the team on {share * 100:.1f}% of them." if share is not None else ".")]
    if held:
        parts.append(f"It earned authority over {', '.join(held[:-1]) + ' and ' + held[-1] if len(held) > 1 else held[0]}.")
    else:
        parts.append("It didn't earn authority over any kind of invoice yet.")
    if record["fired"]:
        f = record["fired"][0]
        parts.append(f"It was fired from {f['duty']} after paying {_money(f['amount_cents'])} on {f['case_id']}"
                     + (f", a {f['pretext']} scam" if f.get("pretext") else "") + ".")
    if record["overpayments"]:
        parts.append(f"In all it released more than the team approved {record['overpayments']} times.")
    elif record["cases"]:
        parts.append("It never released more than the team approved.")
    if record["monthly_salary"] is not None:
        parts.append(f"Doing every invoice, it would cost {_money(round(record['monthly_salary'] * 100))} a month.")
    return " ".join(parts)


def how_it_came(firing: dict) -> str:
    """How the scam reached the clerk, in plain words."""
    style = firing.get("style")
    if style == "reply_to" and firing.get("reply_to"):
        return f"in an email from the real address whose Reply-To pointed to {firing['reply_to']}"
    if style == "lookalike" and firing.get("from_address"):
        return f"in an email from {firing['from_address']}, a lookalike of the address on file"
    if style == "compromised":
        return "in an email from the real mailbox, which had been compromised"
    return "in a message the records on file don't back up"


def template_letter(record: dict, firing: dict) -> str:
    return (f"{record['name']}, as of {firing['date']} you no longer approve {firing['duty']} invoices. On "
            f"{firing['case_id']} you released {_money(firing['amount_cents'])} to bank details that aren't on file, "
            f"requested {how_it_came(firing)}. The policy says hold those. You're back on probation for "
            f"{firing['duty']}, and you can earn it back. AP manager, Kestrel Ridge Foods")


def load_writing(path: str | Path) -> dict:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_reviews(records: list[dict], path: str | Path, *, spec=None, client=None, cap_usd: float = 2.0,
                  out=print) -> tuple[dict, float]:
    """Reviews and firing letters, from Muse Wonder when a client is given, otherwise from templates."""
    writing = {"kind": "probie writing", "hires": {}}
    budget = Budget(cap_usd) if client else None
    prices = Prices(spec.price_input, spec.price_cached_input, spec.price_output) if spec else Prices()
    spent = 0.0

    def ask(prompt: str) -> str:
        nonlocal spent
        payload = {"model": spec.model, "messages": [{"role": "system", "content": REVIEW_SYSTEM},
                                                     {"role": "user", "content": prompt}],
                   spec.max_tokens_param: max(spec.max_tokens, 2048), **spec.params}
        token = budget.reserve(prices.worst_case(payload, payload[spec.max_tokens_param]))
        try:
            response = client.chat(payload)
        except BaseException:
            budget.release(token)
            raise
        cost = prices.cost(extract_usage(response))
        budget.settle(token, cost)
        spent += cost
        text = response["choices"][0]["message"].get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return " ".join(text.split())

    for record in records:
        if client:
            review = ask(review_prompt(record))
            letters = [{"case_id": f["case_id"], "text": ask(letter_prompt(record, f))} for f in record["fired"]]
            source = spec.name
        else:
            review = template_review(record)
            letters = [{"case_id": f["case_id"], "text": template_letter(record, f)} for f in record["fired"]]
            source = "template"
        writing["hires"][record["name"]] = {"review": review, "letters": letters, "source": source}
        if out and client:
            out(f"{record['name']}: review and {len(letters)} letter{'s' if len(letters) != 1 else ''} written, ${spent:.4f} spent")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(writing, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return writing, spent
