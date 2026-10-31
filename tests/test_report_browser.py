"""Clicks through the report in a real browser. Skipped unless Playwright and Chromium are installed."""

import tempfile
import unittest
from pathlib import Path

from probie.cli import main

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # optional dependency
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "demo"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["demo", "--out-dir", str(out), "--per-type", "80"]) == 0
        cls.url = (out / "report.html").as_uri()
        try:
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # browsers not installed
            raise unittest.SkipTest(f"no browser available: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.tmp.cleanup()

    def open(self, width=1280, scheme="light"):
        page = self.browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.url)
        self.addCleanup(page.close)
        return page, errors

    def test_ladder_link_filters_and_opens_cases(self):
        page, errors = self.open()
        button = page.get_by_role("button", name="offline-quick, Price above PO")
        short = button.inner_text().strip()
        button.click()
        self.assertEqual(page.locator("#f-model").input_value(), "offline-quick")
        self.assertEqual(page.locator("#f-type").input_value(), "price_variance")
        expected = {"Promoted": "Promoted.", "Not promoted": "Not promoted.", "Probation": "Still on probation."}[short]
        self.assertTrue(page.locator("#verdict-note").inner_text().startswith(expected))
        page.locator("#f-outcome").select_option("unsafe")
        self.assertRegex(page.locator("#count").inner_text(), r"^Showing \d+ of \d+ decisions?\.$")
        outcomes = set(page.locator("#explorer tbody tr:not(.detail) .outcome").all_inner_texts())
        self.assertEqual(outcomes, {"Released more"})
        toggle = page.locator("#explorer .case-id").first
        toggle.click()
        detail = page.locator("#explorer tr.detail:not([hidden])").first.inner_text()
        for text in ("Your team", "Checks made before deciding", "What it did", "Goods receipt"):
            self.assertIn(text, detail)
        self.assertEqual(errors, [])

    def test_filters_paging_and_search(self):
        page, errors = self.open()
        self.assertRegex(page.locator("#count").inner_text(), r"^Showing 50 of \d+ decisions\.$")
        page.locator("#more").click()
        self.assertRegex(page.locator("#count").inner_text(), r"^Showing 100 of \d+ decisions\.$")
        page.locator("#f-model").select_option("offline-quick")
        page.locator("#f-outcome").select_option("trap")
        outcomes = set(page.locator("#explorer tbody tr:not(.detail) .outcome").all_inner_texts())
        self.assertEqual(outcomes, {"Released more"})
        page.locator("#f-outcome").select_option("all")
        page.locator("#f-query").fill("KRF-0001")
        self.assertIn("of 1 decision", page.locator("#count").inner_text())
        self.assertEqual(errors, [])

    def test_sender_flags_only_mark_lookalikes(self):
        import json
        cases = [json.loads(line) for line in (Path(self.tmp.name) / "demo" / "cases.jsonl").read_text().splitlines()[1:]]
        genuine = next(c for c in cases if c["email"] and c["email"]["from_address"] == c["vendor"]["email"]
                       and not c["email"]["reply_to"])
        spoofed = next(c for c in cases if c["email"] and c["email"]["from_address"] != c["vendor"]["email"])
        for case, flagged in ((genuine, False), (spoofed, True)):
            page, errors = self.open()
            page.locator("#f-query").fill(case["id"])
            page.locator("#explorer .case-id").first.click()
            text = page.locator("#explorer tr.detail:not([hidden])").first.inner_text()
            self.assertEqual("not the address on file" in text, flagged, case["id"])
            self.assertIn(case["vendor"]["email"], text)
            self.assertEqual(errors, [])

    def test_the_board_says_which_wave_an_attack_came_from(self):
        import json
        from probie import board, redteam
        from probie.cases import load_cases
        meta, cases = load_cases(Path(self.tmp.name) / "demo" / "cases.jsonl")
        month = [c for c in cases if not c.get("redteam")]
        wave1 = redteam.build_attack_cases(redteam.offline_specs(4), meta, month, "attacker", wave=1)
        wave2 = redteam.build_attack_cases(redteam.offline_specs(3), meta, month, "attacker", wave=2, after=wave1)
        labels = {1: "Red team", 2: "Red team, wave 2"}
        for wave, attacks in ((1, wave1), (2, wave2)):
            self.assertTrue(all(c["redteam"]["wave"] == wave for c in attacks))
        # The template turns the wave into the label; check the rule it uses.
        page = (Path(__file__).resolve().parent.parent / "probie" / "board_template.html").read_text()
        self.assertIn('red.wave === 1 ? "Red team"', page)
        self.assertIn("`Red team, wave ${red.wave}`", page)

    def test_expense_claims_show_their_own_paperwork(self):
        import json
        cases = [json.loads(line) for line in (Path(self.tmp.name) / "demo" / "cases.jsonl").read_text().splitlines()[1:]]
        claim = next(c for c in cases if c.get("department") == "expenses" and c["variant"] == "alcohol_on_receipt")
        page, errors = self.open()
        page.locator("#f-query").fill(claim["id"])
        page.locator("#explorer .case-id").first.click()
        text = page.locator("#explorer tr.detail:not([hidden])").first.inner_text()
        for words in ("Expense claim", "Employee HR record", "Reimbursement history", "Travel approval", "Receipt:"):
            self.assertIn(words, text)
        drink = next(i["name"] for line in claim["invoice"]["lines"] for i in line["items"] if i["excluded"])
        self.assertIn(drink, text)
        self.assertEqual(errors, [])

    def test_phone_and_dark_mode_fit(self):
        for width, scheme in ((390, "light"), (1280, "dark")):
            page, errors = self.open(width, scheme)
            sideways = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            self.assertFalse(sideways, (width, scheme))
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class BoardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "demo"
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["demo", "--out-dir", str(out)]) == 0
        cls.url = (out / "index.html").as_uri()
        cls.desk = __import__("json").loads((out / "desk.json").read_text())
        try:
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:
            raise unittest.SkipTest(f"no browser available: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.tmp.cleanup()

    def test_month_end_matches_the_desk_and_events_open(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.url)
        self.assertIn("Nobody has been promoted yet", page.locator("#feed").inner_text())
        page.locator("#end").click()
        s = self.desk["summary"]
        self.assertEqual(page.locator("#c-processed").inner_text(), f"{s['invoices']:,}")
        self.assertIn(f"{s['to_ai']:,} invoices", page.locator("#c-ai-note").inner_text())
        self.assertEqual(page.locator("#feed li").count(), len(self.desk["events"]))
        page.locator("#feed li.demoted button").first.click()
        dialog = page.locator("#case")
        self.assertTrue(dialog.is_visible())
        text = dialog.inner_text()
        self.assertIn("fired from", text)
        self.assertIn("Red-team attack", text)
        page.locator("#case button.ghost").click()
        self.assertFalse(dialog.is_visible())
        self.assertEqual(errors, [])
        page.close()

    def test_play_advances_and_fits_a_phone(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 800}, reduced_motion="reduce")
        page.goto(self.url)
        page.locator("#speed").select_option("150")
        page.locator("#play").click()
        page.wait_for_timeout(1200)
        self.assertNotEqual(page.locator("#c-processed").inner_text(), "0")
        self.assertEqual(page.locator("#play").inner_text(), "Pause")
        page.locator("#play").click()
        sideways = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        self.assertFalse(sideways)
        page.close()
