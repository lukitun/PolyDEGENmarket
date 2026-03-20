"""Kelly criterion position sizing calculator."""
import sys
from ledger import get_funds


def kelly(true_prob, market_price, half=True):
    """
    Calculate Kelly criterion bet size.

    true_prob: Our estimated true probability (0-1)
    market_price: Current market price (0-1)
    half: Use half-Kelly (recommended) to reduce variance
    """
    if market_price <= 0 or market_price >= 1:
        return 0

    b = (1 / market_price) - 1  # Net payout per dollar
    q = 1 - true_prob
    fraction = (true_prob * b - q) / b

    if half:
        fraction *= 0.5

    # Hard cap at 20%
    fraction = min(fraction, 0.20)
    fraction = max(fraction, 0)

    funds = get_funds()
    bet_size = funds * fraction

    return {
        "true_prob": true_prob,
        "market_price": market_price,
        "edge": true_prob - market_price,
        "kelly_fraction": fraction,
        "half_kelly": half,
        "bet_size": bet_size,
        "funds": funds,
        "expected_value": true_prob * (1 / market_price) * bet_size - bet_size,
        "max_loss": bet_size,
        "max_gain": bet_size * ((1 / market_price) - 1),
    }


def print_sizing(true_prob, market_price):
    """Print Kelly sizing analysis."""
    full = kelly(true_prob, market_price, half=False)
    half = kelly(true_prob, market_price, half=True)

    print(f"Kelly Criterion Sizing")
    print(f"=" * 40)
    print(f"  True Probability:  {true_prob:.1%}")
    print(f"  Market Price:      ${market_price:.2f}")
    print(f"  Edge:              {full['edge']:.1%}")
    print(f"  Available Funds:   ${full['funds']:.2f}")
    print()
    print(f"  Full Kelly:        {full['kelly_fraction']:.1%} → ${full['bet_size']:.2f}")
    print(f"  Half Kelly:        {half['kelly_fraction']:.1%} → ${half['bet_size']:.2f}")
    print()
    print(f"  Half Kelly Details:")
    print(f"    Bet Size:        ${half['bet_size']:.2f}")
    print(f"    Shares:          {half['bet_size'] / market_price:.1f}")
    print(f"    Max Loss:        -${half['max_loss']:.2f}")
    print(f"    Max Gain:        +${half['max_gain']:.2f}")
    print(f"    Expected Value:  +${half['expected_value']:.2f}")

    if full['edge'] <= 0:
        print(f"\n  WARNING: No edge detected! true_prob <= market_price")
        print(f"  Kelly says: DO NOT BET")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 kelly.py <true_probability> <market_price>")
        print("Example: python3 kelly.py 0.75 0.60")
        sys.exit(1)

    true_prob = float(sys.argv[1])
    market_price = float(sys.argv[2])
    print_sizing(true_prob, market_price)
