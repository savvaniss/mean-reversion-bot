import os
import time
import json
from datetime import datetime, timezone
from typing import Dict, Any, Set

from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
STATUS_FILE = "status.json"

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_config() -> Dict[str, Any]:
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def default_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        pair["name"]: {
            "current_asset": pair["coin_a"]
        }
        for pair in cfg["pairs"]
    }


def load_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        state = default_state(cfg)
        save_state(state)
        return state
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def save_status(status: Dict[str, Any]) -> None:
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def to_symbol(coin: str, stable: str) -> str:
    return f"{coin}{stable}"


def load_tickers(client: Client, symbols: Set[str]) -> Dict[str, float]:
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
    acct = client.get_account()
    balances: Dict[str, float] = {}
    for b in acct["balances"]:
        free_amt = float(b["free"])
        locked_amt = float(b["locked"])
        if free_amt > 0 or locked_amt > 0:
            balances[b["asset"]] = free_amt
    return balances


def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "ERROR: Please set BINANCE_API_KEY and BINANCE_API_SECRET "
            "in your environment or .env file."
        )

    cfg = load_config()
    STABLE = cfg.get("stable_asset", "USDT")
    USE_TESTNET = bool(cfg.get("use_testnet", True))
    DRY_RUN = bool(cfg.get("dry_run", True))
    CHECK_INTERVAL_SEC = int(cfg.get("check_interval_sec", 30))

    client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)

    state = load_state(cfg)

    print("=== Multi-Pair Ratio Bot (python-binance) ===")
    print(f"Stable asset  : {STABLE}")
    print(f"Use testnet   : {USE_TESTNET}")
    print(f"DRY_RUN       : {DRY_RUN}")
    print("Pairs:")
    for p in cfg["pairs"]:
        print(
            f" - {p['name']}: {p['coin_a']}/{p['coin_b']} "
            f"(upper={p['upper_ratio']}, lower={p['lower_ratio']}, "
            f"alloc={p['allocation_pct']})"
        )
    print("Initial state:", state)
    print()

    while True:
        try:
            # reload config each loop (so UI changes take effect)
            cfg = load_config()
            STABLE = cfg.get("stable_asset", "USDT")
            USE_TESTNET = bool(cfg.get("use_testnet", True))
            DRY_RUN = bool(cfg.get("dry_run", True))
            CHECK_INTERVAL_SEC = int(cfg.get("check_interval_sec", 30))
            pairs = cfg["pairs"]

            print("=" * 100)
            print(now_str())

            needed_symbols: Set[str] = set()
            needed_symbols.add(to_symbol("BTC", STABLE))
            for pair in pairs:
                needed_symbols.add(to_symbol(pair["coin_a"], STABLE))
                needed_symbols.add(to_symbol(pair["coin_b"], STABLE))

            tickers = load_tickers(client, needed_symbols)

            btc_symbol = to_symbol("BTC", STABLE)
            btc_price = tickers.get(btc_symbol)
            if btc_price:
                print(f"{btc_symbol}: {btc_price:.6f}")

            free_bal = load_balances(client)

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

            status_out: Dict[str, Any] = {
                "timestamp": now_str(),
                "stable_asset": STABLE,
                "use_testnet": USE_TESTNET,
                "dry_run": DRY_RUN,
                "total_value_stable": total_value_stable,
                "pairs": [],
            }

            for pair in pairs:
                name = pair["name"]
                coin_a = pair["coin_a"]
                coin_b = pair["coin_b"]
                upper = pair["upper_ratio"]
                lower = pair["lower_ratio"]
                alloc_pct = pair["allocation_pct"]

                sym_a = to_symbol(coin_a, STABLE)
                sym_b = to_symbol(coin_b, STABLE)

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

                # collect for status.json (for UI)
                status_out["pairs"].append({
                    "name": name,
                    "coin_a": coin_a,
                    "coin_b": coin_b,
                    "price_a": price_a,
                    "price_b": price_b,
                    "ratio": ratio,
                    "upper_ratio": upper,
                    "lower_ratio": lower,
                    "allocation_pct": alloc_pct,
                    "bal_a": bal_a,
                    "bal_b": bal_b,
                    "bal_stable": bal_stable,
                    "value_pair": value_pair,
                    "max_capital": max_capital,
                    "current_asset": current_asset,
                    "next_plan": next_plan,
                })

                # === EXECUTION (same as before) ===
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
                        try:
                            sell_order = client.order_market_sell(
                                symbol=sym_a,
                                quantity=amount_a_to_sell,
                            )
                            print(f"[{name}] Sell order: {sell_order}")
                        except BinanceAPIException as e:
                            print(f"[{name}] Sell order error: {e}")
                            continue

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

            # write status.json for the UI
            save_status(status_out)

        except Exception as e:
            print("GLOBAL ERROR:", repr(e))

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
