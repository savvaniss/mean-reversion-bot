import os
import time
import json
from datetime import datetime, timezone
from typing import Dict, Any

import ccxt
from dotenv import load_dotenv

# =========================
# LOAD .env CONFIG
# =========================

# Reads .env in current folder (if present)
# Example .env:
# BINANCE_API_KEY=....
# BINANCE_API_SECRET=....
# USE_TESTNET=true
# STABLE_ASSET=USDT
load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() in ("1", "true", "yes")
STABLE = os.getenv("STABLE_ASSET", "USDT")

CHECK_INTERVAL_SEC = 30
DRY_RUN = True  # keep True while testing

STATE_FILE = "state.json"


# =========================
# PAIRS CONFIG
# =========================
# Each pair:
#  - coin_a, coin_b    : two assets that both trade vs STABLE
#  - upper_ratio/lower_ratio: thresholds for HBAR/DOGE-style rotation
#  - allocation_pct    : max fraction of TOTAL portfolio this pair can use

PAIRS = [
    {
        "name": "HBAR_DOGE",
        "coin_a": "HBAR",
        "coin_b": "DOGE",
        "upper_ratio": 1.05,
        "lower_ratio": 0.95,
        "allocation_pct": 0.30,  # 30% of portfolio
    },
    {
        "name": "XRP_XLM",
        "coin_a": "XRP",
        "coin_b": "XLM",
        "upper_ratio": 1.08,
        "lower_ratio": 0.92,
        "allocation_pct": 0.20,  # 20% of portfolio
    },
    # add more pairs if you like
]


# =========================
# STATE HANDLING
# =========================

def default_state() -> Dict[str, Any]:
    return {
        pair["name"]: {
            "current_asset": pair["coin_a"]
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# =========================
# MAIN BOT LOGIC
# =========================

def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "ERROR: Please set BINANCE_API_KEY and BINANCE_API_SECRET "
            "in your environment or .env file."
        )

    exchange = ccxt.binance({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
        },
    })

    # Testnet / mainnet switch
    if USE_TESTNET:
        exchange.set_sandbox_mode(True)

    # Collect all tickers we’ll need (for efficiency)
    needed_symbols = set()
    needed_symbols.add(f"BTC/{STABLE}")
    for pair in PAIRS:
        needed_symbols.add(f"{pair['coin_a']}/{STABLE}")
        needed_symbols.add(f"{pair['coin_b']}/{STABLE}")

    state = load_state()

    print("=== Multi-Pair Ratio Bot ===")
    print(f"Stable asset  : {STABLE}")
    print(f"Use testnet   : {USE_TESTNET}")
    print(f"DRY_RUN       : {DRY_RUN}")
    print("Pairs:")
    for p in PAIRS:
        print(
            f" - {p['name']}: {p['coin_a']}/{p['coin_b']} "
            f"(upper={p['upper_ratio']}, lower={p['lower_ratio']}, "
            f"alloc={p['allocation_pct']})"
        )
    print("Initial state:", state)
    print()

    while True:
        try:
            print("=" * 100)
            print(now_str())

            # ----- Tickers -----
            tickers = exchange.fetch_tickers(list(needed_symbols))

            btc_symbol = f"BTC/{STABLE}"
            btc_price = tickers.get(btc_symbol, {}).get("last")
            if btc_price:
                print(f"{btc_symbol}: {btc_price:.6f}")

            # ----- Balances -----
            balances = exchange.fetch_balance()
            free_bal = balances["free"]

            # Total portfolio in STABLE
            total_value_stable = 0.0
            for asset, amount in free_bal.items():
                if amount <= 0:
                    continue
                if asset == STABLE:
                    total_value_stable += amount
                else:
                    sym = f"{asset}/{STABLE}"
                    if sym in tickers and tickers[sym].get("last"):
                        total_value_stable += amount * tickers[sym]["last"]

            print(f"Estimated total portfolio value: {total_value_stable:.2f} {STABLE}")

            # ----- Each pair -----
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
                    print(f"[{name}] Missing ticker for {sym_a} or {sym_b}, skipping.")
                    continue

                price_a = tickers[sym_a]["last"]
                price_b = tickers[sym_b]["last"]

                if not price_b:
                    print(f"[{name}] price_b is zero/None, skipping.")
                    continue

                ratio = price_a / price_b

                bal_a = free_bal.get(coin_a, 0.0)
                bal_b = free_bal.get(coin_b, 0.0)
                bal_stable = free_bal.get(STABLE, 0.0)

                max_capital = total_value_stable * alloc_pct

                value_a = bal_a * price_a
                value_b = bal_b * price_b
                value_pair = value_a + value_b

                print(
                    f"[{name}] {coin_a}/{STABLE}: {price_a:.6f}, "
                    f"{coin_b}/{STABLE}: {price_b:.6f}, ratio={ratio:.4f}"
                )
                print(
                    f"[{name}] balances: {coin_a}={bal_a:.4f}, "
                    f"{coin_b}={bal_b:.4f}, {STABLE}={bal_stable:.2f}"
                )
                print(
                    f"[{name}] pair value ~ {value_pair:.2f} {STABLE} "
                    f"(max allowed {max_capital:.2f} {STABLE})"
                )

                pair_state = state.get(name, {"current_asset": coin_a})
                current_asset = pair_state["current_asset"]

                next_plan = "HOLD"
                if current_asset == coin_a and ratio > upper:
                    next_plan = f"Switch {coin_a} -> {coin_b} (ratio > upper)"
                elif current_asset == coin_b and ratio < lower:
                    next_plan = f"Switch {coin_b} -> {coin_a} (ratio < lower)"

                print(f"[{name}] current_asset: {current_asset}, next_plan: {next_plan}")

                # ===== EXECUTION =====

                if current_asset == coin_a and ratio > upper:
                    if value_pair <= 0:
                        print(f"[{name}] No {coin_a} value to trade, skipping.")
                        continue

                    trade_value = min(value_pair, max_capital)
                    amount_a_to_sell = min(bal_a, trade_value / price_a)

                    if amount_a_to_sell <= 0:
                        print(f"[{name}] Computed sell amount for {coin_a} is 0, skipping.")
                        continue

                    print(
                        f"[{name}] Trigger: ratio {ratio:.4f} > {upper}, "
                        f"selling {amount_a_to_sell:.6f} {coin_a} for {STABLE} "
                        f"and buying {coin_b}."
                    )

                    if DRY_RUN:
                        print(f"[{name}] [DRY RUN] SELL {sym_a} {amount_a_to_sell:.6f}")
                        print(f"[{name}] [DRY RUN] Then BUY {sym_b} with available {STABLE}")
                    else:
                        sell_order = exchange.create_market_sell_order(sym_a, amount_a_to_sell)
                        print(f"[{name}] Sell order: {sell_order}")

                        balances = exchange.fetch_balance()
                        free_bal = balances["free"]
                        bal_stable = free_bal.get(STABLE, 0.0)

                        stable_for_pair = min(bal_stable, max_capital)
                        if stable_for_pair > 0:
                            amount_b_to_buy = stable_for_pair / price_b
                            buy_order = exchange.create_market_buy_order(sym_b, amount_b_to_buy)
                            print(f"[{name}] Buy order: {buy_order}")
                        else:
                            print(f"[{name}] No {STABLE} after sell, skipping buy.")

                        pair_state["current_asset"] = coin_b
                        state[name] = pair_state
                        save_state(state)

                elif current_asset == coin_b and ratio < lower:
                    if value_pair <= 0:
                        print(f"[{name}] No {coin_b} value to trade, skipping.")
                        continue

                    trade_value = min(value_pair, max_capital)
                    amount_b_to_sell = min(bal_b, trade_value / price_b)

                    if amount_b_to_sell <= 0:
                        print(f"[{name}] Computed sell amount for {coin_b} is 0, skipping.")
                        continue

                    print(
                        f"[{name}] Trigger: ratio {ratio:.4f} < {lower}, "
                        f"selling {amount_b_to_sell:.6f} {coin_b} for {STABLE} "
                        f"and buying {coin_a}."
                    )

                    if DRY_RUN:
                        print(f"[{name}] [DRY RUN] SELL {sym_b} {amount_b_to_sell:.6f}")
                        print(f"[{name}] [DRY RUN] Then BUY {sym_a} with available {STABLE}")
                    else:
                        sell_order = exchange.create_market_sell_order(sym_b, amount_b_to_sell)
                        print(f"[{name}] Sell order: {sell_order}")

                        balances = exchange.fetch_balance()
                        free_bal = balances["free"]
                        bal_stable = free_bal.get(STABLE, 0.0)

                        stable_for_pair = min(bal_stable, max_capital)
                        if stable_for_pair > 0:
                            amount_a_to_buy = stable_for_pair / price_a
                            buy_order = exchange.create_market_buy_order(sym_a, amount_a_to_buy)
                            print(f"[{name}] Buy order: {buy_order}")
                        else:
                            print(f"[{name}] No {STABLE} after sell, skipping buy.")

                        pair_state["current_asset"] = coin_a
                        state[name] = pair_state
                        save_state(state)
                else:
                    print(f"[{name}] No trade condition met, holding.")

                print()

        except Exception as e:
            print("GLOBAL ERROR:", repr(e))

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
