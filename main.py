import os
import time
import json
from datetime import datetime
from typing import Dict, Any, List

import ccxt

# =========================
# CONFIG SECTION
# =========================

STABLE = "USDT"  # use USDT as main trading quote
CHECK_INTERVAL_SEC = 30
DRY_RUN = True        # True = simulate, False = real trades

STATE_FILE = "state.json"

# Pairs config:
# Each pair uses two coins that both trade vs STABLE on Binance spot.
# allocation_pct = fraction of your TOTAL USDT-equivalent portfolio that
# this pair is allowed to use (0.0 – 1.0)
PAIRS = [
    {
        "name": "HBAR_DOGE",
        "coin_a": "HBAR",
        "coin_b": "DOGE",
        "upper_ratio": 1.05,   # when HBAR/DOGE > this and holding HBAR -> switch to DOGE
        "lower_ratio": 0.95,   # when HBAR/DOGE < this and holding DOGE -> switch to HBAR
        "allocation_pct": 0.30 # max 30% of your portfolio in this pair's strategy
    },
    # Later you can add more pairs here like:
    # {
    #   "name": "XRP_XLM",
    #   "coin_a": "XRP",
    #   "coin_b": "XLM",
    #   "upper_ratio": 1.10,
    #   "lower_ratio": 0.90,
    #   "allocation_pct": 0.25
    # }
]


# =========================
# STATE HANDLING
# =========================

def default_state() -> Dict[str, Any]:
    """
    Default: for each pair, assume we start holding coin_a.
    You can edit this file later manually if needed.
    """
    return {
        pair["name"]: {
            "current_asset": pair["coin_a"]  # "HBAR" for HBAR_DOGE
        }
        for pair in PAIRS
    }


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        state = default_state()
        save_state(state)
        return state
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


# =========================
# MAIN BOT LOGIC
# =========================

def main():
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")

    if not api_key or not api_secret:
        raise SystemExit("ERROR: Please set BINANCE_API_KEY and BINANCE_API_SECRET env vars.")

    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot"
        }
    })

    exchange.set_sandbox_mode(True)
    # Precompute all symbols we need: e.g. "HBAR/USDT", "DOGE/USDT"
    needed_symbols = set()
    needed_symbols.add(f"BTC/{STABLE}")  # for display
    for pair in PAIRS:
        needed_symbols.add(f"{pair['coin_a']}/{STABLE}")
        needed_symbols.add(f"{pair['coin_b']}/{STABLE}")

    state = load_state()

    print("=== Multi-Pair Ratio Bot ===")
    print(f"Stable: {STABLE}")
    print(f"DRY_RUN: {DRY_RUN}")
    print("Pairs:")
    for p in PAIRS:
        print(f" - {p['name']}: {p['coin_a']}/{p['coin_b']} "
              f"(upper={p['upper_ratio']}, lower={p['lower_ratio']}, alloc={p['allocation_pct']})")
    print("Initial state:", state)
    print()

    while True:
        try:
            print("=" * 100)
            print(now_str())

            # ----- Fetch all needed tickers -----
            tickers = exchange.fetch_tickers(list(needed_symbols))

            # Get BTC price for panel
            btc_symbol = f"BTC/{STABLE}"
            btc_price = tickers[btc_symbol]["last"] if btc_symbol in tickers else None
            if btc_price:
                print(f"BTC/{STABLE}: {btc_price:.6f}")

            # ----- Fetch balances -----
            balances = exchange.fetch_balance()
            free_bal = balances["free"]

            # Compute total portfolio value in STABLE terms (rough)
            total_value_stable = 0.0
            for asset, amount in free_bal.items():
                if amount <= 0:
                    continue
                if asset == STABLE:
                    total_value_stable += amount
                else:
                    sym = f"{asset}/{STABLE}"
                    if sym in tickers:
                        total_value_stable += amount * tickers[sym]["last"]

            print(f"Estimated total portfolio value: {total_value_stable:.2f} {STABLE}")

            # ----- Iterate over each pair -----
            for pair in PAIRS:
                name = pair["name"]
                coin_a = pair["coin_a"]
                coin_b = pair["coin_b"]
                upper = pair["upper_ratio"]
                lower = pair["lower_ratio"]
                alloc_pct = pair["allocation_pct"]

                sym_a = f"{coin_a}/{STABLE}"
                sym_b = f"{coin_b}/{STABLE}"

                if sym_a not in tickers or sym_b not in tickers:
                    print(f"[{name}] Missing ticker(s) for {sym_a} or {sym_b}, skipping.")
                    continue

                price_a = tickers[sym_a]["last"]
                price_b = tickers[sym_b]["last"]

                if price_b == 0:
                    print(f"[{name}] price_b is zero, skipping.")
                    continue

                ratio = price_a / price_b

                # Get balances for the two coins + stable
                bal_a = free_bal.get(coin_a, 0.0)
                bal_b = free_bal.get(coin_b, 0.0)
                bal_stable = free_bal.get(STABLE, 0.0)

                # Allowed capital for this pair
                max_capital = total_value_stable * alloc_pct

                # Current value in this pair (approx)
                value_a = bal_a * price_a
                value_b = bal_b * price_b
                value_pair = value_a + value_b

                # Panel
                print(f"[{name}] {coin_a}/{STABLE}: {price_a:.6f}, "
                      f"{coin_b}/{STABLE}: {price_b:.6f}, "
                      f"ratio={ratio:.4f}")
                print(f"[{name}] balances: {coin_a}={bal_a:.4f}, {coin_b}={bal_b:.4f}, {STABLE}={bal_stable:.2f}")
                print(f"[{name}] pair value ~ {value_pair:.2f} {STABLE} "
                      f"(max allowed {max_capital:.2f} {STABLE})")

                pair_state = state.get(name, {"current_asset": coin_a})
                current_asset = pair_state["current_asset"]
                next_plan = "HOLD"

                if current_asset == coin_a and ratio > upper:
                    next_plan = f"Switch {coin_a} -> {coin_b} (ratio > upper)"
                elif current_asset == coin_b and ratio < lower:
                    next_plan = f"Switch {coin_b} -> {coin_a} (ratio < lower)"

                print(f"[{name}] current_asset: {current_asset}, next_plan: {next_plan}")

                # ----- EXECUTION LOGIC -----
                if current_asset == coin_a and ratio > upper:
                    # Need to move from coin_a to coin_b
                    if value_pair <= 0:
                        print(f"[{name}] No {coin_a} value to trade, skipping.")
                        continue

                    # Limit to max_capital
                    trade_value = min(value_pair, max_capital)

                    # How much coin_a to sell
                    amount_a_to_sell = min(bal_a, trade_value / price_a)

                    if amount_a_to_sell <= 0:
                        print(f"[{name}] Computed sell amount for {coin_a} is 0, skipping.")
                        continue

                    print(f"[{name}] Trigger: ratio {ratio:.4f} > {upper}, "
                          f"selling {amount_a_to_sell:.6f} {coin_a} for {STABLE} and buying {coin_b}.")

                    if DRY_RUN:
                        print(f"[{name}] [DRY RUN] Would create market SELL {sym_a} {amount_a_to_sell:.6f}")
                        print(f"[{name}] [DRY RUN] Then BUY {sym_b} with available {STABLE}")
                    else:
                        # Sell coin_a -> STABLE
                        sell_order = exchange.create_market_sell_order(sym_a, amount_a_to_sell)
                        print(f"[{name}] Sell order: {sell_order}")

                        # Refresh balances
                        balances = exchange.fetch_balance()
                        free_bal = balances["free"]
                        bal_stable = free_bal.get(STABLE, 0.0)

                        # Use up to 'max_capital' for this pair
                        stable_for_pair = min(bal_stable, max_capital)
                        if stable_for_pair <= 0:
                            print(f"[{name}] No {STABLE} after sell, skipping buy.")
                        else:
                            amount_b_to_buy = stable_for_pair / price_b
                            if amount_b_to_buy > 0:
                                buy_order = exchange.create_market_buy_order(sym_b, amount_b_to_buy)
                                print(f"[{name}] Buy order: {buy_order}")

                        pair_state["current_asset"] = coin_b
                        state[name] = pair_state
                        save_state(state)

                elif current_asset == coin_b and ratio < lower:
                    # Need to move from coin_b to coin_a
                    if value_pair <= 0:
                        print(f"[{name}] No {coin_b} value to trade, skipping.")
                        continue

                    trade_value = min(value_pair, max_capital)
                    amount_b_to_sell = min(bal_b, trade_value / price_b)

                    if amount_b_to_sell <= 0:
                        print(f"[{name}] Computed sell amount for {coin_b} is 0, skipping.")
                        continue

                    print(f"[{name}] Trigger: ratio {ratio:.4f} < {lower}, "
                          f"selling {amount_b_to_sell:.6f} {coin_b} for {STABLE} and buying {coin_a}.")

                    if DRY_RUN:
                        print(f"[{name}] [DRY RUN] Would create market SELL {sym_b} {amount_b_to_sell:.6f}")
                        print(f"[{name}] [DRY RUN] Then BUY {sym_a} with available {STABLE}")
                    else:
                        sell_order = exchange.create_market_sell_order(sym_b, amount_b_to_sell)
                        print(f"[{name}] Sell order: {sell_order}")

                        # Refresh balances
                        balances = exchange.fetch_balance()
                        free_bal = balances["free"]
                        bal_stable = free_bal.get(STABLE, 0.0)

                        stable_for_pair = min(bal_stable, max_capital)
                        if stable_for_pair <= 0:
                            print(f"[{name}] No {STABLE} after sell, skipping buy.")
                        else:
                            amount_a_to_buy = stable_for_pair / price_a
                            if amount_a_to_buy > 0:
                                buy_order = exchange.create_market_buy_order(sym_a, amount_a_to_buy)
                                print(f"[{name}] Buy order: {buy_order}")

                        pair_state["current_asset"] = coin_a
                        state[name] = pair_state
                        save_state(state)

                else:
                    print(f"[{name}] No trade condition met, holding.")

                print()  # blank line per pair

        except Exception as e:
            print("GLOBAL ERROR:", repr(e))

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
