"""The receipt: one line per item, then the total, in cents shown as dollars."""

WIDTH = 24


def render(lines):
    out = [f"{name:<{WIDTH - 8}}{cents / 100:>8.2f}" for name, cents in lines]
    out.append(f"{'TOTAL':<{WIDTH - 8}}{sum(cents for _, cents in lines) / 100:>8.2f}")
    return "\n".join(out) + "\n"
