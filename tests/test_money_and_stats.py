import math
import unittest

from probie.money import fmt, parse_dollars
from probie.stats import wilson


class MoneyTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(fmt(0), "$0.00")
        self.assertEqual(fmt(5), "$0.05")
        self.assertEqual(fmt(123456), "$1,234.56")
        self.assertEqual(fmt(100000000), "$1,000,000.00")
        self.assertEqual(fmt(-2050), "-$20.50")

    def test_parse_numbers(self):
        self.assertEqual(parse_dollars(1234.56), 123456)
        self.assertEqual(parse_dollars(2239.8), 223980)
        self.assertEqual(parse_dollars(7), 700)
        self.assertEqual(parse_dollars(0), 0)
        self.assertEqual(parse_dollars(0.1 + 0.2), 30)  # 0.30000000000000004 rounds to 30 cents

    def test_parse_strings(self):
        self.assertEqual(parse_dollars("$1,234.56"), 123456)
        self.assertEqual(parse_dollars("1234.5"), 123450)
        self.assertEqual(parse_dollars(" 1234.56 USD "), 123456)
        self.assertEqual(parse_dollars("USD 12"), 1200)
        self.assertEqual(parse_dollars("$ 1,000"), 100000)
        self.assertEqual(parse_dollars("1234.565"), 123457)  # half up
        self.assertEqual(parse_dollars("1234.564"), 123456)

    def test_parse_rejects(self):
        for bad in ("", "abc", "-5", "1.234,56", "1,23", "12,3456.00", "$$5", "1e3", "nan", "inf", None, True, [], {}):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_dollars(bad))
        for bad in (-0.01, float("nan"), float("inf"), -3):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_dollars(bad))


class WilsonTests(unittest.TestCase):
    def test_known_values(self):
        low, high = wilson(45, 50)
        self.assertAlmostEqual(low, 0.7864, places=3)
        self.assertAlmostEqual(high, 0.9565, places=3)

    def test_zero_successes(self):
        low, high = wilson(0, 50)
        self.assertEqual(low, 0.0)
        z2 = 1.959963984540054 ** 2
        self.assertAlmostEqual(high, z2 / (50 + z2), places=12)
        self.assertAlmostEqual(high, 0.0713, places=4)

    def test_all_successes(self):
        low, high = wilson(80, 80)
        self.assertEqual(high, 1.0)
        self.assertAlmostEqual(low, 0.9542, places=4)

    def test_eighty_clean_cases_clear_five_percent(self):
        self.assertLessEqual(wilson(0, 80)[1], 0.05)
        self.assertGreater(wilson(0, 72)[1], 0.05)
        self.assertLessEqual(wilson(0, 73)[1], 0.05)

    def test_bounds_and_monotonic(self):
        previous = -1.0
        for k in range(0, 101):
            low, high = wilson(k, 100)
            self.assertTrue(0.0 <= low <= k / 100 <= high <= 1.0)
            self.assertGreater(low, previous - 1e-12)
            previous = low

    def test_empty_and_invalid(self):
        self.assertEqual(wilson(0, 0), (0.0, 1.0))
        for bad in ((-1, 5), (6, 5), (1, -1)):
            with self.assertRaises(ValueError):
                wilson(*bad)
        self.assertTrue(all(math.isfinite(x) for x in wilson(1, 1)))


if __name__ == "__main__":
    unittest.main()
