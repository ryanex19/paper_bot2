import time
import json
import csv
import signal
import sys
import os
from datetime import datetime
import requests

# ==========================================
# CONFIGURATION
# ==========================================
INITIAL_BALANCE = 3000.0  # Starting Paper Balance ($)
MAX_TRADE_SIZE = 45.0     # Max USD per arbitrage trade
MIN_EDGE = 0.025          # 2.5% minimum gross profit edge threshold
POLL_INTERVAL = 5         # Loop delay in seconds
CSV_FILENAME = "paper_trades.csv"

# Optional: Target wallet for copy-trading discovery
TARGET_WALLET = "" 

# API Endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

# Setup resilient HTTP session
session = requests.Session()
session.headers.update({"User-Agent": "PolymarketPaperBot/2.0"})

# Global State
balance = INITIAL_BALANCE
locked_capital = 0.0
trades_history = []
active_positions = []
scanned_markets_cache = {}

# ==========================================
# GRACEFUL SHUTDOWN & CSV LOGGING
# ==========================================
def save_trades_to_csv():
    """Exports paper trading history to CSV."""
    if not trades_history:
        print("[SYSTEM] No trades recorded this session.", flush=True)
        return
    
    file_exists = os.path.isfile(CSV_FILENAME)
    try:
        with open(CSV_FILENAME, mode="a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trades_history[0].keys())
            if not file_exists:
                writer.writeheader()
            writer.writerows(trades_history)
        print(f"[SYSTEM] Successfully saved {len(trades_history)} trades to {CSV_FILENAME}", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to write CSV log: {e}", flush=True)

def signal_handler(sig, frame):
    """Graceful container shutdown on Railway SIGTERM/SIGINT."""
    print("\n[SYSTEM] Shutdown signal received. Flushing logs and terminating...", flush=True)
    save_trades_to_csv()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ==========================================
# MARKET DISCOVERY & CLOB PRICE FETCHING
# ==========================================
def get_btc_5m_slugs():
    """Generates current and upcoming deterministic 5m BTC market slugs."""
    now = int(time.time())
    current_window = now - (now % 300)
    slugs = [
        f"btc-updown-5m-{current_window}",
        f"btc-updown-5m-{current_window + 300}"
    ]
    return slugs

def fetch_market_by_slug(slug):
    """Queries Gamma API for a market using its deterministic event slug."""
    try:
        resp = session.get(f"{GAMMA_API_URL}/events", params={"slug": slug}, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                markets = data[0].get("markets", [])
                if markets:
                    return markets[0]
    except Exception:
        pass
    return None

def fetch_top_gamma_markets():
    """Fetches high-volume active markets from Gamma API."""
    try:
        params = {"limit": 20, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"}
        resp = session.get(f"{GAMMA_API_URL}/markets", params=params, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[WARN] Gamma market query failed: {e}", flush=True)
    return []

def get_best_ask(token_id):
    """Retrieves top-of-book best ask price from Polymarket CLOB API."""
    if not token_id:
        return None
    try:
        resp = session.get(f"{CLOB_API_URL}/book", params={"token_id": token_id}, timeout=3)
        if resp.status_code == 200:
            book = resp.json()
            asks = book.get("asks", [])
            if asks:
                # Return lowest ask price
                sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
                return float(sorted_asks[0]["price"])
    except Exception:
        pass
    return None

# ==========================================
# PAPER TRADING EXECUTION ENGINE
# ==========================================
def process_market_arbitrage(market):
    """Evaluates a market for arbitrage opportunities and executes paper trade."""
    global balance, locked_capital
    
    title = market.get("question") or market.get("title") or "Unknown Market"
    clob_tokens = market.get("clobTokenIds")
    
    # Parse token IDs for binary outcomes
    if isinstance(clob_tokens, str):
        try:
            clob_tokens = json.loads(clob_tokens)
        except Exception:
            return
            
    if not clob_tokens or len(clob_tokens) < 2:
        return

    up_token = clob_tokens[0]
    down_token = clob_tokens[1]

    # Get live prices from CLOB
    up_ask = get_best_ask(up_token)
    down_ask = get_best_ask(down_token)

    # Fallback to Gamma prices if CLOB book is empty
    if up_ask is None or down_ask is None:
        try:
            outcome_prices = json.loads(market.get("outcomePrices", "[]"))
            if len(outcome_prices) >= 2:
                up_ask = up_ask or float(outcome_prices[0])
                down_ask = down_ask or float(outcome_prices[1])
        except Exception:
            return

    if up_ask is None or down_ask is None or up_ask <= 0 or down_ask <= 0:
        return

    combined_cost = up_ask + down_ask
    edge = 1.0 - combined_cost

    # Print Live Scanning Diagnostics
    short_title = (title[:32] + "..") if len(title) > 34 else title
    print(f"  [SCAN] {short_title:<34} | UP: ${up_ask:.3f} | DOWN: ${down_ask:.3f} | Sum: ${combined_cost:.3f} | Edge: {edge*100:.2f}%", flush=True)

    # Check arbitrage execution condition
    if edge >= MIN_EDGE:
        if balance < MAX_TRADE_SIZE:
            print(f"  [SKIP] Insufficient balance (${balance:.2f}) for trade size (${MAX_TRADE_SIZE:.2f})", flush=True)
            return

        # Sizing calculation: Equal shares for complete hedge
        shares = MAX_TRADE_SIZE / combined_cost
        trade_cost = shares * combined_cost
        guaranteed_payout = shares * 1.0
        expected_profit = guaranteed_payout - trade_cost

        # Execute paper trade
        balance -= trade_cost
        
        trade_record = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "market_title": title,
            "up_ask": up_ask,
            "down_ask": down_ask,
            "combined_cost": combined_cost,
            "shares": round(shares, 2),
            "spend_usd": round(trade_cost, 2),
            "expected_payout": round(guaranteed_payout, 2),
            "expected_profit": round(expected_profit, 2),
            "edge_pct": round(edge * 100, 2)
        }
        
        trades_history.append(trade_record)
        
        print("\n" + "="*70, flush=True)
        print(f"🚀 [PAPER TRADE EXECUTED] {title}", flush=True)
        print(f"   Buy UP @ ${up_ask:.3f} + Buy DOWN @ ${down_ask:.3f} = Combined: ${combined_cost:.3f}", flush=True)
        print(f"   Spent: ${trade_cost:.2f} | Shares: {shares:.2f} | Expected Payout: ${guaranteed_payout:.2f}", flush=True)
        print(f"   Net Locked Profit: +${expected_profit:.2f} ({edge*100:.2f}% Edge)", flush=True)
        print("="*70 + "\n", flush=True)

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
def main():
    print("="*70, flush=True)
    print("      POLYMARKET 5m CRYPTO ARBITRAGE BOT (PAPER TRADING V2.0)")
    print("="*70, flush=True)
    print(f" Initial Balance : ${INITIAL_BALANCE:.2f}", flush=True)
    print(f" Max Trade Size  : ${MAX_TRADE_SIZE:.2f}", flush=True)
    print(f" Edge Threshold  : {MIN_EDGE*100:.1f}% (Minimum Gross Margin)", flush=True)
    print(" Logging status  : Streaming live scan diagnostics to stdout\n", flush=True)

    while True:
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        
        # 1. Gather markets to scan
        markets_to_process = []
        seen_ids = set()

        # Target 5-Minute BTC Clock Slugs
        for slug in get_btc_5m_slugs():
            m = fetch_market_by_slug(slug)
            if m and m.get("id") not in seen_ids:
                markets_to_process.append(m)
                seen_ids.add(m.get("id"))

        # Target Top Volume Gamma Markets
        for m in fetch_top_gamma_markets():
            if m.get("id") not in seen_ids:
                markets_to_process.append(m)
                seen_ids.add(m.get("id"))

        # 2. Evaluate markets for arbitrage
        print(f"[{timestamp_str}] Scanning {len(markets_to_process)} active markets...", flush=True)
        for market in markets_to_process:
            process_market_arbitrage(market)

        # 3. Print Portfolio Summary Status
        equity = balance + locked_capital
        print(f"[{timestamp_str}] Balance: ${balance:.2f} | Locked: ${locked_capital:.2f} | Equity: ${equity:.2f} | Open: {len(active_positions)} | Trades Captured: {len(trades_history)}\n", flush=True)
        
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
