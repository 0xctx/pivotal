import unittest

from shop.receipt import render


class ReceiptTest(unittest.TestCase):
    def test_one_line_per_item_then_the_total(self):
        expected = f"{'tea':<16}{3.5:>8.2f}\n{'cake':<16}{12:>8.2f}\n{'TOTAL':<16}{15.5:>8.2f}\n"
        self.assertEqual(render([("tea", 350), ("cake", 1200)]), expected)


if __name__ == "__main__":
    unittest.main()
