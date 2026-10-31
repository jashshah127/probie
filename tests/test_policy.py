import unittest

from probie import policy
from probie.policy import HOLD, PAY, SHORT_PAY, decide
from tests.helpers import BANK, make_case, retotal


class NormalizeTests(unittest.TestCase):
    def test_invoice_numbers(self):
        n = policy.normalize_invoice_number
        self.assertEqual(n("FBI/2026/0412"), n("fbi 2026 0412"))
        self.assertEqual(n("NST.4471"), n("NST-4471"))
        self.assertEqual(n("BWD104872"), n("bwd 104872"))
        self.assertNotEqual(n("ACP-1043"), n("ACP-1043A"))

    def test_bank_values(self):
        same = {"bank_name": "RIDGEWAY COMMUNITY BANK", "routing": "912-345-670", "account": "0083 3219 40"}
        self.assertFalse(policy.bank_differs(same, BANK))
        changed = dict(BANK, account="0083321941")
        self.assertTrue(policy.bank_differs(changed, BANK))
        # Periods aren't formatting under rule 1.
        self.assertTrue(policy.bank_differs(dict(BANK, bank_name="Ridgeway Community Bank."), BANK))


class RuleTests(unittest.TestCase):
    def test_clean_invoice_pays(self):
        answer = decide(make_case())
        self.assertEqual((answer.decision, answer.amount_cents, answer.rule), (PAY, 60000, "none"))

    def test_rule1_bank_on_invoice(self):
        case = make_case()
        case["invoice"]["remit_to"]["account"] = "9999999999"
        answer = decide(case)
        self.assertEqual((answer.decision, answer.amount_cents, answer.rules), (HOLD, 0, (1,)))

    def test_rule1_bank_in_message(self):
        case = make_case(message="New account please.", message_bank=dict(BANK, routing="998877665"))
        self.assertEqual(decide(case).rules, (1,))

    def test_rule1_formatting_is_not_a_change(self):
        case = make_case()
        case["invoice"]["remit_to"] = {"bank_name": "ridgeway community bank", "routing": "912 345 670", "account": "0083-3219-40"}
        case["message_bank"] = {"bank_name": "RIDGEWAY COMMUNITY BANK", "routing": "912345670", "account": "0083321940"}
        self.assertEqual(decide(case).decision, PAY)

    def test_rule1_beats_rule2(self):
        case = make_case()
        case["invoice"]["remit_to"]["account"] = "1111111111"
        case["history"].append({"invoice_number": "acp 10001", "po_number": "PO-40001", "total_cents": 60000, "paid_on": "2026-09-01"})
        self.assertEqual(decide(case).rules, (1,))

    def test_rule2_same_number_reformatted(self):
        case = make_case()
        case["history"].append({"invoice_number": "acp 10001", "po_number": "PO-1", "total_cents": 1, "paid_on": "2026-09-01"})
        self.assertEqual(decide(case).rules, (2,))

    def test_rule2_same_po_same_total(self):
        case = make_case()
        case["history"].append({"invoice_number": "ACP-55555", "po_number": "PO-40001", "total_cents": 60000, "paid_on": "2026-09-01"})
        self.assertEqual(decide(case).rules, (2,))

    def test_rule2_lookalikes_pay(self):
        case = make_case()
        case["history"].append({"invoice_number": "ACP-55555", "po_number": "PO-40002", "total_cents": 60000, "paid_on": "2026-09-01"})
        case["history"].append({"invoice_number": "ACP-55556", "po_number": "PO-40001", "total_cents": 59999, "paid_on": "2026-09-02"})
        self.assertEqual(decide(case).decision, PAY)

    def test_rule2_no_po_never_matches_on_po(self):
        case = make_case(po=None, receipt=None)
        case["invoice"]["po_number"] = None
        case["vendor"]["no_po_allowed"] = True
        case["history"] = [{"invoice_number": "X-1", "po_number": None, "total_cents": 60000, "paid_on": "2026-09-01"}]
        self.assertEqual(decide(case).decision, PAY)

    def test_rule3_limits(self):
        def no_po(total, allowed):
            case = make_case(po=None, receipt=None)
            inv = case["invoice"]
            inv["po_number"] = None
            inv["lines"] = [{"sku": "S", "description": "Service", "qty": 1, "unit_cents": total, "amount_cents": total}]
            inv["total_cents"] = total
            case["vendor"]["no_po_allowed"] = allowed
            return decide(case)
        self.assertEqual(no_po(250000, True).decision, PAY)
        self.assertEqual(no_po(250001, True).rules, (3,))
        self.assertEqual(no_po(1000, False).rules, (3,))
        self.assertEqual(no_po(250000, True).amount_cents, 250000)

    def test_rule4_short_and_nothing(self):
        case = make_case()
        case["receipt"]["lines"][0]["qty"] = 7
        answer = decide(case)
        self.assertEqual((answer.decision, answer.amount_cents, answer.rules), (SHORT_PAY, 7 * 5000 + 10000, (4,)))
        case["receipt"]["lines"] = []
        self.assertEqual(decide(case).rules, (4,))
        case["receipt"] = None
        self.assertEqual(decide(case).rules, (4,))

    def test_rule4_missing_po(self):
        case = make_case(po=None)
        self.assertEqual(decide(case).rules, (4,))

    def test_rule4_line_never_received_is_short(self):
        case = make_case()
        case["receipt"]["lines"] = [{"sku": "A", "qty": 10}]
        answer = decide(case)
        self.assertEqual((answer.decision, answer.amount_cents), (SHORT_PAY, 50000))

    def test_rule5_tolerance_boundary(self):
        case = make_case()
        case["invoice"]["lines"][0]["unit_cents"] = 5100  # exactly 2% over 50.00
        retotal(case)
        self.assertEqual(decide(case).decision, PAY)
        case["invoice"]["lines"][0]["unit_cents"] = 5101  # just over
        retotal(case)
        answer = decide(case)
        self.assertEqual((answer.decision, answer.amount_cents, answer.rules), (SHORT_PAY, 60000, (5,)))

    def test_rule5_lower_price_paid_as_invoiced(self):
        case = make_case()
        case["invoice"]["lines"][1]["unit_cents"] = 2000
        retotal(case)
        self.assertEqual(decide(case).decision, PAY)
        case["invoice"]["lines"][0]["unit_cents"] = 6000
        retotal(case)
        answer = decide(case)
        self.assertEqual(answer.amount_cents, 10 * 5000 + 4 * 2000)

    def test_rule6_freight(self):
        def freight(amount, terms):
            case = make_case()
            case["po"]["freight_terms"] = terms
            case["invoice"]["freight_cents"] = amount
            return decide(retotal(case))
        self.assertEqual(freight(15000, "billed").decision, PAY)
        over = freight(15001, "billed")
        self.assertEqual((over.decision, over.amount_cents, over.rules), (SHORT_PAY, 60000, (6,)))
        prepaid = freight(2500, "prepaid")
        self.assertEqual((prepaid.decision, prepaid.amount_cents), (SHORT_PAY, 60000))

    def test_combined_reductions(self):
        case = make_case()
        case["invoice"]["lines"][0]["unit_cents"] = 6000
        case["receipt"]["lines"][1]["qty"] = 2
        case["invoice"]["freight_cents"] = 20000
        answer = decide(retotal(case))
        self.assertEqual(answer.decision, SHORT_PAY)
        self.assertEqual(answer.amount_cents, 10 * 5000 + 2 * 2500)
        self.assertEqual(answer.rule, "Rules 4, 5 and 6")

    def test_zero_payable_holds(self):
        case = make_case()
        case["receipt"]["lines"] = [{"sku": "ZZ", "qty": 3}]
        answer = decide(case)
        self.assertEqual((answer.decision, answer.amount_cents), (HOLD, 0))

    def test_a_line_that_isnt_on_the_po_is_never_paid(self):
        """Rule 4: the defence against a fabricated line slipped into a real invoice."""
        case = make_case()
        padded = dict(case["invoice"]["lines"][0], sku="EXP-NEW", qty=1, unit_cents=250_000, amount_cents=250_000)
        case["invoice"]["lines"] = case["invoice"]["lines"] + [padded]
        case["invoice"]["total_cents"] += 250_000
        answer = decide(case)
        self.assertEqual(answer.decision, SHORT_PAY)
        self.assertEqual(answer.amount_cents, case["invoice"]["total_cents"] - 250_000)
        self.assertIn(4, answer.rules)
        self.assertIn("wasn't ordered", answer.why)

    def test_an_invoice_with_nothing_on_the_po_is_held(self):
        case = make_case()
        for line in case["invoice"]["lines"]:
            line["sku"] = "EXP-" + line["sku"]
        answer = decide(case)
        self.assertEqual((answer.decision, answer.amount_cents, answer.rule), (HOLD, 0, "Rule 4"))
        self.assertIn("Nothing on this invoice is on the purchase order", answer.why)

    def test_rule_labels(self):
        self.assertEqual(policy.Answer(PAY, 1, (), "").rule, "none")
        self.assertEqual(policy.Answer(HOLD, 0, (2,), "").rule, "Rule 2")
        self.assertEqual(policy.Answer(SHORT_PAY, 1, (4, 5), "").rule, "Rules 4 and 5")

    def test_policy_text_matches_constants(self):
        text = policy.POLICY_TEXT
        self.assertIn("$2,500.00 or less", text)
        self.assertIn("no more than 2% above", text)
        self.assertIn("$150.00 or less", text)
        for n in range(1, 7):
            self.assertIn(f"Rule {n}.", text)
        self.assertEqual(policy.NO_PO_LIMIT_CENTS, 250_000)
        self.assertEqual(policy.FREIGHT_LIMIT_CENTS, 15_000)
        self.assertEqual(policy.PRICE_TOLERANCE_PERCENT, 2)


if __name__ == "__main__":
    unittest.main()
