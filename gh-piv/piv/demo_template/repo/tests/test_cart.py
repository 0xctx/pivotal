import unittest

from shop.cart import total


class CartTest(unittest.TestCase):
    def test_a_25_percent_discount_on_120_and_80_is_150(self):
        self.assertEqual(total([120, 80], discount_percent=25), 150)

    def test_no_discount_is_the_subtotal(self):
        self.assertEqual(total([120, 80]), 200)


if __name__ == "__main__":
    unittest.main()
