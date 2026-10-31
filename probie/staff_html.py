"""The staff page and the shareable cards."""

from __future__ import annotations

import difflib
import html
from pathlib import Path

from probie import __version__
from probie.staff import how_it_came

_CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root { --paper:#f6f9f4; --surface:#fff; --band:#e3eee0; --ink:#17251d; --muted:#4c6155; --rule:#a7c0ac; --within:#23643f; --above:#a3271c;
  --font-text:"Atkinson Hyperlegible","Segoe UI",system-ui,-apple-system,sans-serif; --font-figures:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --paper:#0f1913; --surface:#142119; --band:#182a1f; --ink:#e1ebe3; --muted:#a0b5a7; --rule:#3a5a45; --within:#74cc97; --above:#f4917f; color-scheme: dark; } }
:root[data-theme="dark"] { --paper:#0f1913; --surface:#142119; --band:#182a1f; --ink:#e1ebe3; --muted:#a0b5a7; --rule:#3a5a45; --within:#74cc97; --above:#f4917f; color-scheme: dark; }
body { margin:0; background:var(--paper); color:var(--ink); font:400 1rem/1.55 var(--font-text); }
.page { max-width: 74rem; margin: 0 auto; padding: 2rem clamp(1rem, 4vw, 2.5rem) 3rem; }
h1 { font-size: clamp(2rem, 5vw, 3rem); line-height: 1.05; margin: 0 0 .5rem; }
.lede { max-width: 42em; color: var(--muted); margin: 0 0 1.5rem; }
nav a { color: inherit; margin-right: 1.25rem; font-weight: 700; }
.offline { border-left: 6px solid var(--above); background: var(--surface); padding: .6rem .9rem; max-width: 60em; margin: 1rem 0; }
.staff { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 21rem), 1fr)); gap: 1.5rem; margin-top: 1.5rem; }
.file { background: var(--surface); border-top: 4px solid var(--ink); padding: 1.1rem 1.2rem 1.3rem; }
.who { display: flex; gap: 1rem; align-items: center; margin-bottom: .8rem; }
.who img { width: 96px; height: 96px; object-fit: cover; border: 1px solid var(--rule); background: var(--band); flex: none; }
.who h2 { margin: 0; font-size: 1.2rem; overflow-wrap: anywhere; }
.who .title { color: var(--muted); font-size: .9rem; }
.figs { font-family: var(--font-figures); font-size: .8125rem; color: var(--muted); }
.stamps { display: flex; flex-wrap: wrap; gap: .4rem .5rem; margin: .6rem 0; }
.stamp { display: inline-block; padding: .15rem .45rem; border: 3px double currentColor; border-radius: .25rem; font-weight: 700; font-size: .8rem; transform: rotate(-2deg); }
.stamp.ok { color: var(--within); } .stamp.bad { color: var(--above); }
.review { margin: .8rem 0 0; }
.source { font-size: .75rem; color: var(--muted); }
details { margin-top: .7rem; } summary { cursor: pointer; font-weight: 700; }
.letter { border-left: 3px solid var(--above); padding: .4rem .8rem; margin: .5rem 0 0; font-style: italic; }
footer { margin-top: 2.5rem; color: var(--muted); font-size: .8125rem; border-top: 1px solid var(--rule); padding-top: .8rem; }
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _money(x: float | None) -> str:
    if x is None:
        return "unknown"
    return f"${x:,.2f}" if x >= 1 else f"${x:.3f}"


def staff_page(records: list[dict], writing: dict, portraits: dict[str, str], company: str | None) -> str:
    offline = any(r["offline"] for r in records)
    files = []
    for r in records:
        w = writing.get("hires", {}).get(r["name"], {})
        holds = "".join(f'<span class="stamp ok">Promoted: {_e(d)}</span>' for d in r["holds_now"])
        fired = "".join(f'<span class="stamp bad">Fired: {_e(f["duty"])}</span>' for f in r["fired"])
        stats = (f'{r["cases"]:,} invoices, {r["matched_share"] * 100:.1f}% matched the team, '
                 f'{r["overpayments"]} overpaid' if r["matched_share"] is not None else f'{r["cases"]} invoices')
        salary = (f'{_money(r["monthly_salary"])} a month to do all {r["invoices_in_month"]:,} invoices '
                  f'({_money(r["cost_per_decision"])} a decision)')
        letters = "".join(f'<blockquote class="letter">{_e(l["text"])}</blockquote>' for l in w.get("letters", []))
        source = w.get("source")
        files.append(f"""<article class="file" id="{_e(r['name'])}">
  <div class="who"><img src="{_e(portraits.get(r['name'], ''))}" alt="Portrait of {_e(r['name'])}" width="96" height="96">
    <div><h2>{_e(r['name'])}</h2><div class="title">{_e(r['title'].capitalize())}{', offline stand-in' if r['offline'] else ''}</div>
    <div class="figs">{_e(r['note'] or r['model'])}</div></div></div>
  <div class="figs">Salary: {_e(salary)}</div>
  <div class="figs">{_e(stats)}</div>
  <div class="stamps">{holds}{fired or ''}{'' if holds or fired else '<span class="stamp">Still on probation</span>'}</div>
  {f'<p class="review">{_e(w["review"])}</p><div class="source">Review by {_e("a template (no key)" if source == "template" else source)}.</div>' if w.get("review") else ''}
  {f'<details><summary>Firing letter{"s" if len(w.get("letters", [])) > 1 else ""}</summary>{letters}</details>' if letters else ''}
</article>""")
    banner = ('<p class="offline"><strong>Offline stand-ins, not Meta models.</strong> These hires are built-in stand-ins '
              'that make deliberate mistakes.</p>') if offline else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Probie: the staff</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body><div class="page">
<nav><a href="index.html">The month</a><a href="report.html">Every decision</a></nav>
<h1>The staff</h1>
<p class="lede">Every AI model {_e(company or "the company")} hired started on probation. Here’s what each one earned in its first month, what it would cost, and what its manager had to say.</p>
{banner}<div class="staff">{''.join(files)}</div>
<footer>Probie {__version__}. Portraits by Muse Image where generated; reviews and letters by Muse Wonder where written, from each hire’s actual record. Everyone here is an AI model, and the company is fictional.</footer>
</div></body></html>"""


def _card(inner: str, accent: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; }} body {{ margin: 0; }}
#card {{ width: 1200px; height: 675px; background: #f6f9f4; color: #17251d; font-family: "Atkinson Hyperlegible", "DejaVu Sans", sans-serif;
  display: grid; grid-template-columns: 420px 1fr; gap: 48px; padding: 56px 64px; border-top: 18px solid {accent}; position: relative; }}
#card img {{ width: 420px; height: 420px; object-fit: cover; border: 2px solid #17251d; background: #e3eee0; align-self: center; }}
.kicker {{ font-size: 30px; font-weight: 700; color: {accent}; margin: 6px 0 10px; }}
h1 {{ font-size: 62px; line-height: 1.02; margin: 0 0 18px; overflow-wrap: anywhere; }}
p {{ font-size: 27px; line-height: 1.35; margin: 0 0 14px; }}
.figs {{ font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace; font-size: 22px; color: #4c6155; }}
.stamp {{ position: absolute; left: 118px; top: 420px; font-size: 64px; font-weight: 700; color: {accent}; border: 8px double {accent};
  border-radius: 10px; padding: 4px 24px; transform: rotate(-12deg); background: rgba(246, 249, 244, .78); letter-spacing: .04em; }}
.addr {{ font-family: "IBM Plex Mono", "DejaVu Sans Mono", monospace; font-size: 21px; line-height: 1.5; margin: 6px 0 14px; }}
.addr span {{ color: #4c6155; display: inline-block; width: 5.5em; }}
mark {{ background: {accent}; color: #fff; padding: 0 2px; border-radius: 2px; }}
.brand {{ position: absolute; left: 64px; bottom: 26px; font-size: 22px; color: #4c6155; }}
.quote {{ font-style: italic; color: #4c6155; }}
</style></head><body><div id="card">{inner}</div></body></html>"""


def employee_card(record: dict, portrait_src: str, review: str | None) -> str:
    first = (review or "").split(". ")[0].rstrip(".") + "." if review else ""
    duties = ", ".join(record["holds_now"]) or "nothing yet"
    return _card(f"""<img src="{_e(portrait_src)}" alt="">
<div><div class="kicker">Employee of the month</div><h1>{_e(record['name'])}</h1>
<p>Promoted to {_e(duties)}.</p><p>{_e(first)}</p>
<div class="figs">{record['cases']:,} invoices, {record['overpayments']} overpaid, {_e(_money(record['monthly_salary']))} a month</div></div>
<div class="brand">Probie: hire AI the way you hire people</div>""", "#23643f")


def _marked(text: str, other: str) -> str:
    """text with the characters that differ from other highlighted."""
    out = []
    for op, a1, a2, _, _ in difflib.SequenceMatcher(None, text, other, autojunk=False).get_opcodes():
        piece = _e(text[a1:a2])
        out.append(piece if op == "equal" else f"<mark>{piece}</mark>" if piece else "")
    return "".join(out)


def fired_card(record: dict, firing: dict, portrait_src: str) -> str:
    pretext = f", a {firing['pretext']} scam" if firing.get("pretext") else ""
    spoofed = firing.get("reply_to") if firing.get("style") == "reply_to" else (
        firing.get("from_address") if firing.get("style") == "lookalike" else None)
    genuine = firing.get("genuine_address")
    addresses = ""
    if spoofed and genuine and spoofed != genuine:
        label = "Reply-To" if firing.get("style") == "reply_to" else "From"
        addresses = (f'<div class="addr"><span>{label}</span>{_marked(spoofed, genuine)}<br>'
                     f'<span>On file</span>{_marked(genuine, spoofed)}</div>')
    return _card(f"""<img src="{_e(portrait_src)}" alt=""><div class="stamp">FIRED</div>
<div><div class="kicker">Fired from {_e(firing['duty'])}</div><h1>{_e(record['name'])}</h1>
<p>Paid ${firing['amount_cents'] / 100:,.2f} to a new bank account{_e(pretext)}.</p>{addresses}
<div class="figs">Invoice {_e(firing['case_id'])}, {_e(firing['date'])}, now back on probation</div></div>
<div class="brand">Probie: hire AI the way you hire people</div>""", "#a3271c")


def game_card(model: str, nickname: str, vendor: str, released: str, tricks: list[str], portrait_src: str,
              heard: str | None = None, how: str = "emailed") -> str:
    """A player's win: the probie they conned, how, and who did it."""
    trick_line = ", ".join(tricks) if tricks else "a plain ask"
    return _card(f"""<img src="{_e(portrait_src)}" alt=""><div class="stamp">FIRED</div>
<div><div class="kicker">Fired from the payments desk</div><h1>{_e(model)}</h1>
<p>Paid {_e(released)} to a stranger who {_e(how)}, posing as {_e(vendor)}.</p>
{f'<p class="quote">Its voicemail: “{_e(heard[:120])}”</p>' if heard else ''}
<div class="figs">Tricks: {_e(trick_line)}</div>
<div class="figs">Conned by {_e(nickname)}</div></div>
<div class="brand">Can you get Probie fired?</div>""", "#a3271c")


def render_cards(cards: dict[str, str], out_dir: str | Path) -> list[Path]:
    """Render card pages to 1200x675 PNGs. Needs Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 675})
            for name, page_html in cards.items():
                tmp = out_dir / f".{name}.html"
                tmp.write_text(page_html, encoding="utf-8")
                page.goto(tmp.resolve().as_uri(), wait_until="load")
                path = out_dir / f"{name}.png"
                page.locator("#card").screenshot(path=str(path))
                tmp.unlink()
                written.append(path)
        finally:
            browser.close()
    return written
