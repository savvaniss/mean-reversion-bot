import os
import time
import json
from datetime import datetime, timezone
from typing import Dict, Any, Set

from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# =========================
# LOAD .env CONFIG
# =========================

# .env example:
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


def to_symbol(coin: str) -> str:
    """Convert coin + STABLE to Binance symbol, e.g. HBAR -> HBARUSDT."""
    return f"{coin}{STABLE}"


def load_tickers(client: Client, symbols: Set[str]) -> Dict[str, float]:
    """
    Fetch latest prices for the given Binance symbols (e.g. 'HBARUSDT').
    Returns dict {symbol: price_float}.
    """
    prices: Dict[str, float] = {}
    for sym in symbols:
        try:
            t = client.get_symbol_ticker(symbol=sym)
            prices[sym] = float(t["price"])
        except BinanceAPIException as e:
            print(f"Ticker error for {sym}: {e}")
        except Exception as e:
            print(f"Unknown ticker error for {sym}: {e}")
    return prices


def load_balances(client: Client) -> Dict[str, float]:
    """
    Return dict {asset: free_balance_float}.
    """
    acct = client.get_account()
    balances: Dict[str, float] = {}
    for b in acct["balances"]:
        free_amt = float(b["free"])
        locked_amt = float(b["locked"])
        if free_amt > 0 or locked_amt > 0:
            balances[b["asset"]] = free_amt
    return balances


# =========================
# MAIN BOT LOGIC
# =========================

def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "ERROR: Please set BINANCE_API_KEY and BINANCE_API_SECRET "
            "in your environment or .env file."
        )

    # python-binance client (handles testnet correctly)
    client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)

    # Collect all Binance symbols we’ll need
    needed_symbols: Set[str] = set()
    needed_symbols.add(to_symbol("BTC"))
    for pair in PAIRS:
        needed_symbols.add(to_symbol(pair["coin_a"]))
        needed_symbols.add(to_symbol(pair["coin_b"]))

    state = load_state()

    print("=== Multi-Pair Ratio Bot (python-binance) ===")
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
            tickers = load_tickers(client, needed_symbols)

            btc_symbol = to_symbol("BTC")
            btc_price = tickers.get(btc_symbol)
            if btc_price:
                print(f"{btc_symbol}: {btc_price:.6f}")

            # ----- Balances -----
            free_bal = load_balances(client)

            # Total portfolio in STABLE
            total_value_stable = 0.0
            for asset, amount in free_bal.items():
                if amount <= 0:
                    continue
                if asset == STABLE:
                    total_value_stable += amount
                else:
                    sym = f"{asset}{STABLE}"
                    px = tickers.get(sym)
                    if px:
                        total_value_stable += amount * px

            print(f"Estimated total portfolio value: {total_value_stable:.2f} {STABLE}")

            # ----- Each pair -----
            for pair in PAIRS:
                name = pair["name"]
                coin_a = pair["coin_a"]
                coin_b = pair["coin_b"]
                upper = pair["upper_ratio"]
                lower = pair["lower_ratio"]
                alloc_pct = pair["allocation_pct"]

                sym_a = to_symbol(coin_a)
                sym_b = to_symbol(coin_b)

                price_a = tickers.get(sym_a)
                price_b = tickers.get(sym_b)

                if price_a is None or price_b is None:
                    print(f"[{name}] Missing ticker for {sym_a} or {sym_b}, skipping.")
                    continue

                if price_b == 0:
                    print(f"[{name}] price_b is zero, skipping.")
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

                # From coin_a -> coin_b
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
                        # Market sell coin_a for STABLE
                        try:
                            sell_order = client.order_market_sell(
                                symbol=sym_a,
                                quantity=amount_a_to_sell,
                            )
                            print(f"[{name}] Sell order: {sell_order}")
                        except BinanceAPIException as e:
                            print(f"[{name}] Sell order error: {e}")
                            continue

                        # Refresh balances after sell
                        free_bal = load_balances(client)
                        bal_stable = free_bal.get(STABLE, 0.0)

                        stable_for_pair = min(bal_stable, max_capital)
                        if stable_for_pair > 0:
                            amount_b_to_buy = stable_for_pair / price_b
                            try:
                                buy_order = client.order_market_buy(
                                    symbol=sym_b,
                                    quantity=amount_b_to_buy,
                                )
                                print(f"[{name}] Buy order: {buy_order}")
                            except BinanceAPIException as e:
                                print(f"[{name}] Buy order error: {e}")
                        else:
                            print(f"[{name}] No {STABLE} after sell, skipping buy.")

                        pair_state["current_asset"] = coin_b
                        state[name] = pair_state
                        save_state(state)

                # From coin_b -> coin_a
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
                        # Market sell coin_b for STABLE
                        try:
                            sell_order = client.order_market_sell(
                                symbol=sym_b,
                                quantity=amount_b_to_sell,
                            )
                            print(f"[{name}] Sell order: {sell_order}")
                        except BinanceAPIException as e:
                            print(f"[{name}] Sell order error: {e}")
                            continue

                        free_bal = load_balances(client)
                        bal_stable = free_bal.get(STABLE, 0.0)

                        stable_for_pair = min(bal_stable, max_capital)
                        if stable_for_pair > 0:
                            amount_a_to_buy = stable_for_pair / price_a
                            try:
                                buy_order = client.order_market_buy(
                                    symbol=sym_a,
                                    quantity=amount_a_to_buy,
                                )
                                print(f"[{name}] Buy order: {buy_order}")
                            except BinanceAPIException as e:
                                print(f"[{name}] Buy order error: {e}")
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
