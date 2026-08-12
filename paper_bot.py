import json
import logging
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

# Set up clean logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


class PolymarketCryptoFetcher:
    """Fetches, parses, and cleans Polymarket crypto binary market data

    via public REST APIs (Gamma API and CLOB API).
    """

    GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
    CLOB_BASE_URL = "https://clob.polymarket.com"

    def __init__(self, session: requests.Session = None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "PolymarketCryptoDataPipeline/1.0", "Accept": "application/json"}
        )

    def fetch_crypto_events(
        self, tag_id: int = 21, limit: int = 100, active_only: bool = True
    ) -> list:
        """Fetch crypto prediction events from the Gamma API.

        Tag ID 21 is reserved for Crypto markets on Polymarket.
        """
        endpoint = f"{self.GAMMA_BASE_URL}/events"
        params = {
            "tag_id": tag_id,
            "limit": limit,
            "active": str(active_only).lower(),
            "closed": "false" if active_only else "true",
            "order": "volume24hr",
            "ascending": "false",
        }

        try:
            logger.info(
                f"Fetching top {limit} crypto events from Gamma API..."
            )
            resp = self.session.get(endpoint, params=params, timeout=10)
            resp.raise_for_status()
            events = resp.json()
            logger.info(f"Successfully retrieved {len(events)} crypto events.")
            return events
        except requests.RequestException as e:
            logger.error(f"Failed to fetch events from Gamma API: {e}")
            return []

    def fetch_clob_orderbook(self, token_id: str) -> dict:
        """Fetch the current L2 Order Book depth from CLOB API for a specific token."""
        endpoint = f"{self.CLOB_BASE_URL}/book"
        params = {"token_id": token_id}

        try:
            resp = self.session.get(endpoint, params=params, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(
                f"Could not fetch order book for token {token_id}: {e}"
            )
            return {"bids": [], "asks": []}

    @staticmethod
    def _safe_json_parse(val, default=None):
        """Helper to parse JSON string representations returned by Polymarket APIs."""
        if isinstance(val, (list, dict)):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
        return default if default is not None else []

    def process_market_data(self, events: list) -> pd.DataFrame:
        """Flattens nested events/markets payload into a structured, clean pandas DataFrame

        with normalized timestamps, derived probabilities, and liquidity metrics.
        """
        records = []

        for event in events:
            event_id = event.get("id")
            event_title = event.get("title", "").strip()
            event_slug = event.get("slug", "")

            markets = event.get("markets", [])
            for market in markets:
                # Basic Metadata
                market_id = market.get("id")
                condition_id = market.get("conditionId")
                question = market.get("question", "").strip()
                description = market.get("description", "").strip()
                slug = market.get("slug", "")

                # Parses stringified JSON fields safely
                outcomes = self._safe_json_parse(
                    market.get("outcomes"), ["Yes", "No"]
                )
                prices = self._safe_json_parse(market.get("outcomePrices"), [])
                clob_tokens = self._safe_json_parse(
                    market.get("clobTokenIds"), []
                )

                # Parse implied probability (assuming Yes / No binary outcomes)
                yes_price = float(prices[0]) if len(prices) > 0 else np.nan
                no_price = float(prices[1]) if len(prices) > 1 else np.nan

                yes_token_id = clob_tokens[0] if len(clob_tokens) > 0 else None
                no_token_id = clob_tokens[1] if len(clob_tokens) > 1 else None

                # Extract Best Bids / Asks provided directly by Gamma
                best_bid = (
                    float(market["bestBid"])
                    if market.get("bestBid") is not None
                    else np.nan
                )
                best_ask = (
                    float(market["bestAsk"])
                    if market.get("bestAsk") is not None
                    else np.nan
                )
                last_trade = (
                    float(market["lastTradePrice"])
                    if market.get("lastTradePrice") is not None
                    else np.nan
                )

                # Calculate Bid-Ask Spread
                spread = (
                    best_ask - best_bid
                    if not np.isnan(best_ask) and not np.isnan(best_bid)
                    else np.nan
                )

                # Volumes & Liquidity
                volume_24h = (
                    float(market.get("volume24hr", 0))
                    if market.get("volume24hr")
                    else 0.0
                )
                total_volume = (
                    float(market.get("volume", 0))
                    if market.get("volume")
                    else 0.0
                )
                liquidity = (
                    float(market.get("liquidity", 0))
                    if market.get("liquidity")
                    else 0.0
                )

                # Parse ISO Timestamps into UTC Datetime objects
                created_at = pd.to_datetime(
                    market.get("createdAt"), errors="coerce", utc=True
                )
                end_date = pd.to_datetime(
                    market.get("endDate"), errors="coerce", utc=True
                )

                # Append clean record
                records.append(
                    {
                        "event_id": event_id,
                        "market_id": market_id,
                        "condition_id": condition_id,
                        "event_title": event_title,
                        "question": question,
                        "slug": slug,
                        "active": market.get("active", False),
                        "closed": market.get("closed", False),
                        "yes_implied_prob": yes_price,
                        "no_implied_prob": no_price,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": spread,
                        "last_trade_price": last_trade,
                        "volume_24h_usd": volume_24h,
                        "total_volume_usd": total_volume,
                        "liquidity_usd": liquidity,
                        "yes_token_id": yes_token_id,
                        "no_token_id": no_token_id,
                        "created_at": created_at,
                        "end_date": end_date,
                    }
                )

        df = pd.DataFrame(records)

        # Post-Processing & Filtering
        if not df.empty:
            # Sort by 24h Trading Volume
            df = df.sort_values(
                by="volume_24h_usd", ascending=False
            ).reset_index(drop=True)

        return df


# --- Execution Example ---
if __name__ == "__main__":
    fetcher = PolymarketCryptoFetcher()

    # 1. Fetch live events
    events = fetcher.fetch_crypto_events(tag_id=21, limit=20, active_only=True)

    # 2. Process into a structured dataframe
    df_crypto = fetcher.process_market_data(events)

    print("\n--- Polymarket Top Crypto Prediction Markets ---")
    print(
        df_crypto[
            [
                "question",
                "yes_implied_prob",
                "best_bid",
                "best_ask",
                "volume_24h_usd",
                "liquidity_usd",
            ]
        ].head(10)
    )

    # 3. Fetch L2 orderbook snapshot for the top market's "YES" token
    if not df_crypto.empty and df_crypto["yes_token_id"].iloc[0]:
        top_token = df_crypto["yes_token_id"].iloc[0]
        top_question = df_crypto["question"].iloc[0]

        print(f"\n--- Fetching Order Book for Top Market: '{top_question}' ---")
        orderbook = fetcher.fetch_clob_orderbook(top_token)
        print(f"Top 3 Bids: {orderbook.get('bids', [])[:3]}")
        print(f"Top 3 Asks: {orderbook.get('asks', [])[:3]}")
