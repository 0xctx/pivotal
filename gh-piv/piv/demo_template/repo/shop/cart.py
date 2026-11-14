"""The cart: what the items cost after a percentage discount, in cents."""


def total(prices, discount_percent=0):
    subtotal = sum(prices)
    # Seeded bug: the discount is subtracted as cents, not taken as a percentage of the subtotal.
    return subtotal - discount_percent
