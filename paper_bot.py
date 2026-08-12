import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# ====================== SETTINGS ======================
STARTING_BALANCE = 3000.0
MAX_PER_MARKET = 45.0  # Max $ to spend per market
MAX_OPEN_MARKETS = 3
MIN_EDGE = 0.04  # 4% minimum edge (combined ask <= 0.96)
POLL_SECONDS = 5

TARGET = os.getenv(
    "TARGET_WALLET", "0x251c1a283703beed41590b0875a8dcb8ddd1541f"
).lower()
# ======================================================

# Endpoints
DATA_API = "https://data-api.polymarket.com/activity"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"

# Persistent HTTP Session for connection pooling & low latency
session = requests.Session()

# State variables
balance = STARTING_BALANCE
locked_capital = 0.0
open_positions = []  # list of open position dicts
trade_history = []  # list of completed trade dicts
seen_markets = set()


def get_best_ask(token_id):
    """Fetch best ask price from the order book for realistic paper fills."""
    try:
        r = session.get(CLOB_BOOK, params={"token_id": token_id}, timeout=4)
        if r.status_code == 200:
            data = r.json()
            asks = data.get("asks", [])
            if asks:
                return float(asks[0]["price"])
    except requests.RequestException:
        pass
    return None


# ==================== DUAL MARKET DISCOVERY ====================


def discover_from_target_wallet(markets_dict):
    """Discovery Method 1: Get active BTC markets from Target Wallet trades."""
    params = {
        "user": TARGET,
        "type": "TRADE",
        "limit": 40,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }
    try:
        r = session.get(DATA_API, params=params, timeout=10)
        r.raise_for_status()
        trades = r.json()

        for t in trades:
            title = str(t.get("title", "")).lower()
            if (
                "bitcoin" not in title
                and "btc" not in title
                and "up or down" not in title
            ):
                continue

            cid = t.get("conditionId")
            asset = t.get("asset")
            outcome = str(t.get("outcome", "")).capitalize()

            if not cid or not asset or outcome not in ("Up", "Down"):
                continue

            if cid not in markets_dict:
                markets_dict[cid] = {
                    "Up": None,
                    "Down": None,
                    "title": t.get("title", ""),
                }

            markets_dict[cid][outcome] = asset
            markets_dict[cid]["title"] = t.get("title", "")

    except requests.RequestException as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Target Wallet API Error: {e}", flush=True)


def discover_from_gamma(markets_dict):
    """Discovery Method 2: Get active BTC 5-minute/Up-Down markets from Polymarket Gamma API."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": 50,
        "order": "id",
        "ascending": "false",
    }
    try:
        r = session.get(GAMMA_API, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        for m in data:
            title = str(m.get("question") or m.get("title", "")).lower()
            if "bitcoin" not in title and "btc" not in title:
                continue
            if (
                "up or down" not in title
                and "5-minute" not in title
                and "5m" not in title
            ):
                continue

            cid = m.get("conditionId")
            if not cid:
                continue

            tokens_raw = m.get("clobTokenIds")
            outcomes_raw = m.get("outcomes")

            tokens = (
                json.loads(tokens_raw)
                if isinstance(tokens_raw, str)
                else (tokens_raw or [])
            )
            outcomes = (
                json.loads(outcomes_raw)
                if isinstance(outcomes_raw, str)
                else (outcomes_raw or [])
            )

            if len(tokens) < 2:
                continue

            up_token, down_token = None, None
            for idx, outcome_name in enumerate(outcomes):
                name = str(outcome_name).capitalize()
                if name in ("Up", "Yes"):
                    up_token = tokens[idx]
                elif name in ("Down", "No"):
                    down_token = tokens[idx]

            if not up_token and len(tokens) >= 1:
                up_token = tokens[0]
            if not down_token and len(tokens) >= 2:
                down_token = tokens[1]

            if cid not in markets_dict:
                markets_dict[cid] = {
                    "Up": None,
                    "Down": None,
                    "title": m.get("question") or m.get("title", ""),
                }

            if up_token:
                markets_dict[cid]["Up"] = up_token
            if down_token:
                markets_dict[cid]["Down"] = down_token
            markets_dict[cid]["title"] = m.get("question") or m.get("title", "")

    except requests.RequestException as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Gamma API Error: {e}", flush=True)


def discover_markets():
    """Combines Target Wallet Activity and Gamma API into a single market dictionary."""
    markets = {}
    discover_from_target_wallet(markets)
    discover_from_gamma(markets)
    return markets


# ===============================================================


def try_open_position(cid, info):
    global balance, locked_capital

    up_token = info.get("Up")
    down_token = info.get("Down")
    title = info.get("title", "")[:55]

    if (
        not up_token
        or not down_token
        or cid in seen_markets
        or len(open_positions) >= MAX_OPEN_MARKETS
    ):
        return

    up_ask = get_best_ask(up_token)
    down_ask = get_best_ask(down_token)

    if up_ask is None or down_ask is None:
        return

    combined_cost = up_ask + down_ask
    edge = 1.0 - combined_cost

    if edge < MIN_EDGE:
        return

    # Capital allocation safety check
    spend = min(MAX_PER_MARKET, balance * 0.4)
    if spend < 15 or (balance - spend) < 500:
        return

    # Equal Share Sizing: Buy identical share counts on both sides for risk-free arbitrage
    shares = spend / combined_cost
    up_cost = shares * up_ask
    down_cost = shares * down_ask
    total_spend = up_cost + down_cost

    position = {
        "id": cid,
        "title": title,
        "open_time": datetime.now(timezone.utc),
        "up_ask": up_ask,
        "down_ask": down_ask,
        "combined": combined_cost,
        "edge": edge,
        "spent": total_spend,
        "shares": shares,
        "status": "OPEN",
    }

    open_positions.append(position)
    seen_markets.add(cid)

    balance -= total_spend
    locked_capital += total_spend

    print("\n" + "=" * 70, flush=True)
    print("PAPER TRADE OPENED - ARBITRAGE", flush=True)
    print(f"Market   : {title}", flush=True)
    print(f"Up Ask   : {up_ask:.4f} | Down Ask: {down_ask:.4f}", flush=True)
    print(f"Combined : {combined_cost:.4f} | Guaranteed Edge: {edge*100:.2f}%", flush=True)
    print(f"Shares   : {shares:.2f} pairs | Spent: ${total_spend:.2f}", flush=True)
    print(f"Free Bal : ${balance:.2f} | Locked: ${locked_capital:.2f}", flush=True)
    print("=" * 70 + "\n", flush=True)


def check_resolutions():
    global balance, locked_capital

    now = datetime.now(timezone.utc)
    still_open = []

    for pos in open_positions:
        age = (now - pos["open_time"]).total_seconds() / 60

        if age >= 5.5:  # Simulate settlement after ~5.5 minutes
            payout = pos["shares"] * 1.0  # Each paired share pays $1.00 guaranteed
            profit = payout - pos["spent"]

            balance += payout
            locked_capital -= pos["spent"]

            pos["status"] = "CLOSED"
            pos["payout"] = payout
            pos["profit"] = profit
            pos["close_time"] = now

            trade_history.append(pos)

            print("\n" + "-" * 70, flush=True)
            print("PAPER POSITION RESOLVED", flush=True)
            print(f"Market : {pos['title']}", flush=True)
            print(
                f"Spent  : ${pos['spent']:.2f} → Guaranteed Payout: ${payout:.2f}",
                flush=True,
            )
            print(f"Profit : ${profit:+.2f}", flush=True)
            print(f"Balance: ${balance:.2f}", flush=True)
            print("-" * 70 + "\n", flush=True)
        else:
            still_open.append(pos)

    open_positions[:] = still_open


def print_status():
    total_equity = balance + locked_capital
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"Balance: ${balance:.2f} | Locked: ${locked_capital:.2f} | "
        f"Equity: ${total_equity:.2f} | Open: {len(open_positions)} | "
        f"Trades: {len(trade_history)}",
        flush=True,
    )


def save_summary_and_exit(signum=None, frame=None):
    """Saves trade history and prints summary when Railway stops or restarts the worker."""
    print("\n" + "=" * 70, flush=True)
    print("SHUTTING DOWN / SAVING PAPER TRADES HISTORY", flush=True)
    print("=" * 70, flush=True)
    print(f"Starting Balance : ${STARTING_BALANCE:,.2f}", flush=True)
    print(f"Final Balance    : ${balance:,.2f}", flush=True)
    print(f"Total Profit     : ${balance - STARTING_BALANCE:+,.2f}", flush=True)
    print(f"Total Trades     : {len(trade_history)}", flush=True)

    if trade_history:
        wins = sum(1 for t in trade_history if t["profit"] > 0)
        print(
            f"Win Rate         : {wins}/{len(trade_history)} ({wins/len(trade_history)*100:.1f}%)",
            flush=True,
        )

        df = pd.DataFrame(trade_history)
        filename = (
            f"paper_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        df.to_csv(filename, index=False)
        print(f"Saved trade history → {filename}", flush=True)

    print("=" * 70, flush=True)
    sys.exit(0)


def main():
    # Register shutdown handlers for Railway container lifecycle management
    signal.signal(signal.SIGINT, save_summary_and_exit)
    signal.signal(signal.SIGTERM, save_summary_and_exit)

    print("=" * 70, flush=True)
    print("PAPER TRADING BOT STARTED - 24/7 MODE", flush=True)
    print(f"Starting Balance : ${STARTING_BALANCE:,.2f}", flush=True)
    print(f"Max per market   : ${MAX_PER_MARKET}", flush=True)
    print(f"Min Edge         : {MIN_EDGE*100:.1f}%", flush=True)
    print(f"Target Wallet    : {TARGET}", flush=True)
    print("=" * 70 + "\n", flush=True)

    # Infinite 24/7 Execution Loop
    while True:
        try:
            markets = discover_markets()

            for cid, info in markets.items():
                try_open_position(cid, info)

            check_resolutions()
            print_status()

            time.sleep(POLL_SECONDS)

        except Exception as e:
            # Catch transient network/API issues to keep the 24/7 worker alive
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Unexpected Loop Error: {e}",
                flush=True,
            )
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()