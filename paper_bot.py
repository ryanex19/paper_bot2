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
MAX_TRADE_SIZE = 45.0     # Max USD spent per arbitrage trade
MIN_EDGE = 0.025          # 2.5% minimum gross edge (0.025)
MIN_DEPTH_USD = 2.00      # Ignore orders with less than $2 available liquidity
POLL_INTERVAL = 5         # Seconds between market scans
CSV_FILENAME = "paper_trades.csv"

# Keywords used to ensure we ONLY scan crypto-related markets
CRYPTO_KEYWORDS = ["btc", "eth", "sol", "bitcoin", "ethereum", "updown", "up/down", "5m", "15m", "crypto"]

# API Endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

# Setup resilient HTTP session
session = requests.Session()
session.headers.update({"User-Agent": "PolymarketCryptoPaperBot/4.0"})

# Global State
balance = INITIAL_BALANCE
trades_history = []

# ==========================================
# GRACEFUL SHUTDOWN & LOGGING
# ==========================================
def save_trades_to_csv():
    """Flushes recorded paper trades to CSV upon shutdown."""
    if not trades_history:
        print("[SYSTEM] No trades recorded in this session.", flush=True)
        return
    
    file_exists = os.path.isfile(CSV_FILENAME)
    try:
        with open(CSV_FILENAME, mode="a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trades_history[0].keys())
            if not file_exists:
                writer.writeheader()
            writer.writerows(trades_history)
        print(f"[SYSTEM] Successfully exported {len(trades_history)} trades to {CSV_FILENAME}", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to save CSV file: {e}", flush=True)

def signal_handler(sig, frame):
    """Handles container SIGTERM / SIGINT for safe Railway shutdown."""
    print("\n[SYSTEM] Termination signal received. Saving logs...", flush=True)
    save_trades_to_csv()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ==========================================
# MARKET & CLOB PRICE FETCHING
# ==========================================
def is_crypto_market(market):
    """Filters out non-crypto markets (e.g. politics, pop culture, sports)."""
    title = (market.get("question") or market.get("title") or "").lower()
    return any(keyword in title for keyword in CRYPTO_KEYWORDS)

def get_btc_5m_slugs():
    """Generates current and upcoming deterministic 5m BTC market slugs."""
    now = int(time.time())
    current_window = now - (now % 300)
    return [
        f"btc-updown-5m-{current_window}",
        f"btc-updown-5m-{current_window + 300}"
    ]

def fetch_market_by_slug(slug):
    """Fetches market metadata from Gamma API via event slug."""
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

def fetch_top_gamma_crypto_markets():
    """Fetches high-volume markets using Tag 21 (Crypto) from Gamma API."""
    try:
        params = {
            "tag_id": 21,  # Tag 21 is reserved for Crypto on Polymarket
            "limit": 20, 
            "active": "true", 
            "closed": "false", 
            "order": "volume24hr", 
            "ascending": "false"
        }
        resp = session.get(f"{GAMMA_API_URL}/markets", params=params, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[WARN] Gamma market query failed: {e}", flush=True)
    return []

def get_best_ask(token_id):
    """
    Fetches the best ask price from Polymarket CLOB book,
    filtering out unrealistic/dust orders with insufficient depth.
    """
    if not token_id:
        return None
    try:
        resp = session.get(f"{CLOB_API_URL}/book", params={"token_id": token_id}, timeout=3)
        if resp.status_code == 200:
            book = resp.json()
            asks = book.get("asks", [])
            if asks:
                sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
                
                # Filter for asks with real depth and price >= $0.02
                for ask in sorted_asks:
                    price = float(ask["price"])
                    size = float(ask["size"])
                    if price >= 0.02 and (price * size) >= MIN_DEPTH_USD:
                        return price
    except Exception:
        pass
    return None

# ==========================================
# TRADE EXECUTION ENGINE
# ==========================================
def process_market_arbitrage(market):
    """Evaluates market pricing, filters phantom edges, and executes paper trade."""
    global balance
    
    title = market.get("question") or market.get("title") or "Unknown Market"
    clob_tokens = market.get("clobTokenIds")
    
    if isinstance(clob_tokens, str):
        try:
            clob_tokens = json.loads(clob_tokens)
        except Exception:
            return
            
    if not clob_tokens or len(clob_tokens) < 2:
        return

    up_token = clob_tokens[0]
    down_token = clob_tokens[1]

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

    short_title = (title[:30] + "..") if len(title) > 32 else title

    # SANITY GUARD: Filter phantom/corrupt order books (< $0.92 sum)
    if combined_cost < 0.92:
        print(f"  [REJECT] {short_title:<32} | Phantom edge (Sum: ${combined_cost:.3f}). Corrupt order book skipped.", flush=True)
        return

    # Print Live Scanning Output
    print(f"  [SCAN]   {short_title:<32} | UP: ${up_ask:.3f} | DOWN: ${down_ask:.3f} | Sum: ${combined_cost:.3f} | Edge: {edge*100:.2f}%", flush=True)

    # ARBITRAGE ENTRY CONDITION
    if edge >= MIN_EDGE:
        if balance < MAX_TRADE_SIZE:
            print(f"  [SKIP] Balance (${balance:.2f}) lower than required trade size (${MAX_TRADE_SIZE:.2f})", flush=True)
            return

        # Sizing Calculations
        shares = MAX_TRADE_SIZE / combined_cost
        trade_cost = shares * combined_cost
        guaranteed_payout = shares * 1.0  # 1 UP share + 1 DOWN share converts to $1.00
        net_profit = guaranteed_payout - trade_cost

        # BALANCE UPDATE: Instantly deduct purchase cost AND credit guaranteed payout
        balance = balance - trade_cost + guaranteed_payout

        # Record Trade Details
        trade_record = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "market_title": title,
            "up_ask": round(up_ask, 3),
            "down_ask": round(down_ask, 3),
            "combined_cost": round(combined_cost, 3),
            "shares": round(shares, 2),
            "spend_usd": round(trade_cost, 2),
            "payout_usd": round(guaranteed_payout, 2),
            "net_profit": round(net_profit, 2),
            "edge_pct": round(edge * 100, 2),
            "new_balance": round(balance, 2)
        }
        
        trades_history.append(trade_record)

        print("\n" + "="*70, flush=True)
        print(f"🚀 [ARBITRAGE EXECUTED] {title}", flush=True)
        print(f"   Buy UP @ ${up_ask:.3f} + Buy DOWN @ ${down_ask:.3f} = Combined Cost: ${combined_cost:.3f}", flush=True)
        print(f"   Spent: ${trade_cost:.2f} | Shares Received: {shares:.2f} | Instant Payout: ${guaranteed_payout:.2f}", flush=True)
        print(f"   Net Realized Profit: +${net_profit:.2f} ({edge*100:.2f}% Edge)", flush=True)
        print(f"   Updated Balance: ${balance:.2f}", flush=True)
        print("="*70 + "\n", flush=True)

# ==========================================
# MAIN LOOP
# ==========================================
def main():
    print("="*70, flush=True)
    print("      POLYMARKET CRYPTO ARBITRAGE BOT (PRODUCTION V4.0)")
    print("="*70, flush=True)
    print(f" Initial Balance : ${INITIAL_BALANCE:.2f}", flush=True)
    print(f" Trade Cap       : ${MAX_TRADE_SIZE:.2f}", flush=True)
    print(f" Minimum Edge    : {MIN_EDGE*100:.1f}%", flush=True)
    print(f" Minimum Depth   : ${MIN_DEPTH_USD:.2f}\n", flush=True)

    while True:
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        markets_to_process = []
        seen_ids = set()

        # 1. Target 5m BTC Clock Slugs
        for slug in get_btc_5m_slugs():
            m = fetch_market_by_slug(slug)
            if m and m.get("id") not in seen_ids:
                markets_to_process.append(m)
                seen_ids.add(m.get("id"))

        # 2. Target High-Volume Crypto Markets (Filtering out politics/sports)
        for m in fetch_top_gamma_crypto_markets():
            if m.get("id") not in seen_ids and is_crypto_market(m):
                markets_to_process.append(m)
                seen_ids.add(m.get("id"))

        print(f"[{timestamp_str}] Scanning {len(markets_to_process)} active crypto markets...", flush=True)
        for market in markets_to_process:
            process_market_arbitrage(market)

        print(f"[{timestamp_str}] Available Cash: ${balance:.2f} | Total Trades Executed: {len(trades_history)}\n", flush=True)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
