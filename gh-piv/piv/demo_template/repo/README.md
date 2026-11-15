# piv-demo

A sandbox for Pivotal (`piv demo`). It is public, it is safe to break, and `piv demo --clean` deletes it.

- `shop/cart.py` has a seeded bug: the discount is taken as cents, not as a percentage.
- `tests/test_cart.py` is red until that bug is fixed.
- `shop/receipt.py` prints a receipt; one of the issues asks for a rule above its total.

Run the tests with:

```
python3 -m unittest discover -s tests
```
