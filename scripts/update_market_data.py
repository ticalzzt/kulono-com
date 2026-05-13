#!/usr/bin/env python3
"""
Kulono Market Data Updater
Fetches live market data from free APIs and generates market-data.json.
Supports --push flag to auto-commit and push to git.

Data sources (primary → fallback):
  - Stock indices: Stooq → yfinance
  - Forex: Frankfurter (ECB) → yfinance
  - Crypto: CoinGecko → Stooq (btc.v/eth.v) → yfinance
  - Bond yields: yfinance (may not work from mainland China)
"""

import json
import sys
import subprocess
import time
import urllib.request
from datetime import datetime, timezone, timedelta

# ─── Config ───

STOOQ_INDICES = {
    "SP500":  {"symbol": "^spx",   "name": "S&P 500"},
    "NASDAQ": {"symbol": "^ndq",   "name": "Nasdaq Composite"},
    "NIKKEI": {"symbol": "^nkx",   "name": "Nikkei 225"},
    "HSI":    {"symbol": "^hsi",   "name": "Hang Seng Index"},
}

YFINANCE_INDICES = {
    "CSI300": {"ticker": "000300.SS", "name": "CSI 300"},
    "STOXX":  {"ticker": "^STOXX50E", "name": "STOXX Europe 50"},
}

FOREX = {
    "EUR_USD": {"base": "EUR", "quote": "USD", "name": "EUR/USD", "yf_ticker": "EURUSD=X"},
    "USD_JPY": {"base": "USD", "quote": "JPY", "name": "USD/JPY", "yf_ticker": "JPY=X"},
    "GBP_USD": {"base": "GBP", "quote": "USD", "name": "GBP/USD", "yf_ticker": "GBPUSD=X"},
    "USD_CNY": {"base": "USD", "quote": "CNY", "name": "USD/CNY", "yf_ticker": "CNY=X"},
}

BONDS = {
    "US10Y": {"ticker": "^TNX", "name": "US 10-Year Treasury"},
    "US2Y":  {"ticker": "^IRX", "name": "US 2-Year Treasury"},
}

OUTPUT_FILE = "market-data.json"

# ─── Helpers ───

def safe_round(val, digits=2):
    try:
        return round(float(val), digits)
    except (TypeError, ValueError):
        return None


def fetch_url(url, timeout=15):
    """Fetch a URL with basic error handling."""
    req = urllib.request.Request(url, headers={"User-Agent": "kulono-market-updater/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


# ─── Stooq Fetcher ───

def fetch_stooq_quote(symbol):
    """Fetch latest quote from Stooq CSV API. Returns dict with close, prev, or None."""
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcp&h&e=csv"
    try:
        content = fetch_url(url)
        lines = content.strip().split("\r\n")
        if len(lines) >= 2:
            parts = lines[1].split(",")
            if len(parts) >= 8 and parts[3] != "N/D":
                return {
                    "close": float(parts[6]),  # Close
                    "prev": float(parts[7]) if parts[7] != "N/D" else None,  # Prev Close
                }
    except Exception as e:
        print(f"    ⚠ Stooq {symbol}: {e}")
    return None


def fetch_indices_stooq():
    """Fetch indices from Stooq."""
    result = {}
    for key, info in STOOQ_INDICES.items():
        quote = fetch_stooq_quote(info["symbol"])
        if quote:
            change = None
            if quote["prev"] and quote["prev"] != 0:
                change = ((quote["close"] - quote["prev"]) / quote["prev"]) * 100
            result[key] = {
                "value": safe_round(quote["close"], 2),
                "change": safe_round(change, 2),
                "name": info["name"],
            }
        else:
            result[key] = {"value": None, "change": None, "name": info["name"]}
        time.sleep(0.3)  # Be polite
    return result


# ─── yfinance Fetcher ───

def fetch_indices_yfinance(tickers_dict):
    """Fetch indices from yfinance. May not work from mainland China."""
    try:
        import yfinance as yf
    except ImportError:
        return {k: {"value": None, "change": None, "name": v["name"]} for k, v in tickers_dict.items()}

    result = {}
    tickers_str = " ".join(v["ticker"] for v in tickers_dict.values())
    try:
        data = yf.download(tickers_str, period="2d", progress=False, auto_adjust=True)
        for key, info in tickers_dict.items():
            try:
                close_prices = data["Close"][info["ticker"]].dropna()
                if len(close_prices) >= 2:
                    current = close_prices.iloc[-1]
                    prev = close_prices.iloc[-2]
                    change_pct = ((current - prev) / prev) * 100
                elif len(close_prices) == 1:
                    current = close_prices.iloc[-1]
                    change_pct = 0.0
                else:
                    result[key] = {"value": None, "change": None, "name": info["name"]}
                    continue
                result[key] = {
                    "value": safe_round(current, 2),
                    "change": safe_round(change_pct, 2),
                    "name": info["name"],
                }
            except Exception:
                result[key] = {"value": None, "change": None, "name": info["name"]}
    except Exception:
        result = {k: {"value": None, "change": None, "name": v["name"]} for k, v in tickers_dict.items()}

    return result


# ─── Combined Index Fetcher ───

def fetch_all_indices():
    """Fetch all indices: Stooq primary + yfinance for the rest."""
    print("  Fetching from Stooq (primary)...")
    stooq_result = fetch_indices_stooq()
    
    # Fill in yfinance-only indices
    if YFINANCE_INDICES:
        print("  Fetching from yfinance (CSI300, STOXX)...")
        yf_result = fetch_indices_yfinance(YFINANCE_INDICES)
        stooq_result.update(yf_result)
    
    # For any Stooq indices that failed, try yfinance
    failed_stooq = {k: v for k, v in STOOQ_INDICES.items() 
                    if stooq_result.get(k, {}).get("value") is None}
    if failed_stooq:
        print(f"  Retrying {len(failed_stooq)} failed tickers via yfinance...")
        yf_retry = fetch_indices_yfinance(failed_stooq)
        for k, v in yf_retry.items():
            if v["value"] is not None:
                stooq_result[k] = v
    
    return stooq_result


# ─── Forex Fetcher ───

def fetch_forex_frankfurter():
    """Fetch forex rates from Frankfurter API (ECB, free, no key)."""
    content = fetch_url("https://api.frankfurter.dev/v1/latest")
    raw = json.loads(content)
    rates = raw["rates"]
    rates["EUR"] = 1.0

    result = {}
    for key, info in FOREX.items():
        base_rate = rates.get(info["base"])
        quote_rate = rates.get(info["quote"])
        if base_rate and quote_rate:
            cross = quote_rate / base_rate
            result[key] = {"value": safe_round(cross, 4), "name": info["name"]}
        else:
            result[key] = {"value": None, "name": info["name"]}
    return result


def fetch_forex_yfinance():
    """Fallback: fetch forex rates via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        return {k: {"value": None, "name": v["name"]} for k, v in FOREX.items()}

    result = {}
    tickers_str = " ".join(v["yf_ticker"] for v in FOREX.values())
    try:
        data = yf.download(tickers_str, period="1d", progress=False, auto_adjust=True)
        for key, info in FOREX.items():
            try:
                close = data["Close"][info["yf_ticker"]].dropna()
                val = close.iloc[-1] if len(close) > 0 else None
                result[key] = {"value": safe_round(val, 4), "name": info["name"]}
            except Exception:
                result[key] = {"value": None, "name": info["name"]}
    except Exception:
        result = {k: {"value": None, "name": v["name"]} for k, v in FOREX.items()}
    return result


def fetch_forex():
    """Fetch forex rates, Frankfurter → yfinance fallback."""
    try:
        print("  Fetching from Frankfurter (ECB)...")
        result = fetch_forex_frankfurter()
        if any(v["value"] is not None for v in result.values()):
            print("  ✓ Frankfurter OK")
            return result, "Frankfurter"
    except Exception as e:
        print(f"  ⚠ Frankfurter failed: {e}")

    print("  Falling back to yfinance for forex...")
    result = fetch_forex_yfinance()
    return result, "yfinance"


# ─── Crypto Fetcher ───

def fetch_crypto_coingecko():
    """Fetch crypto global data from CoinGecko."""
    content = fetch_url("https://api.coingecko.com/api/v3/global")
    raw = json.loads(content)
    data = raw["data"]
    return {
        "total_market_cap": safe_round(data["total_market_cap"]["usd"], 0),
        "btc_dominance": safe_round(data["market_cap_percentage"]["btc"] / 100, 4),
        "btc_price": None,
        "eth_price": None,
    }


def fetch_crypto_stooq():
    """Fetch BTC/ETH prices from Stooq."""
    btc = fetch_stooq_quote("btc.v")
    time.sleep(0.3)
    eth = fetch_stooq_quote("eth.v")
    return {
        "total_market_cap": None,
        "btc_dominance": None,
        "btc_price": safe_round(btc["close"], 0) if btc else None,
        "eth_price": safe_round(eth["close"], 0) if eth else None,
    }


def fetch_crypto_yfinance():
    """Fallback: fetch crypto prices via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        return {"total_market_cap": None, "btc_dominance": None, "btc_price": None, "eth_price": None}

    result = {"total_market_cap": None, "btc_dominance": None, "btc_price": None, "eth_price": None}
    try:
        data = yf.download("BTC-USD ETH-USD", period="1d", progress=False, auto_adjust=True)
        try:
            btc_close = data["Close"]["BTC-USD"].dropna()
            if len(btc_close) > 0:
                result["btc_price"] = safe_round(btc_close.iloc[-1], 0)
        except Exception:
            pass
        try:
            eth_close = data["Close"]["ETH-USD"].dropna()
            if len(eth_close) > 0:
                result["eth_price"] = safe_round(eth_close.iloc[-1], 0)
        except Exception:
            pass
    except Exception:
        pass
    return result


def fetch_crypto():
    """Fetch crypto: CoinGecko → Stooq → yfinance."""
    # Try CoinGecko first for full data
    try:
        print("  Fetching from CoinGecko...")
        result = fetch_crypto_coingecko()
        # Supplement BTC/ETH prices from Stooq
        print("  Supplementing BTC/ETH prices from Stooq...")
        stooq = fetch_crypto_stooq()
        if stooq["btc_price"]:
            result["btc_price"] = stooq["btc_price"]
        if stooq["eth_price"]:
            result["eth_price"] = stooq["eth_price"]
        print("  ✓ CoinGecko + Stooq OK")
        return result, "CoinGecko"
    except Exception as e:
        print(f"  ⚠ CoinGecko failed: {e}")

    # Fallback to Stooq only
    print("  Fetching BTC/ETH from Stooq...")
    result = fetch_crypto_stooq()
    if result["btc_price"] is not None:
        print("  ✓ Stooq crypto OK")
        return result, "Stooq"

    # Last resort: yfinance
    print("  Falling back to yfinance for crypto...")
    result = fetch_crypto_yfinance()
    return result, "yfinance"


# ─── Bond Fetcher ───

def fetch_bonds_yfinance():
    """Fetch Treasury yields via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        return {k: {"value": None, "name": v["name"]} for k, v in BONDS.items()}

    result = {}
    tickers_str = " ".join(v["ticker"] for v in BONDS.values())
    try:
        data = yf.download(tickers_str, period="1d", progress=False, auto_adjust=True)
        for key, info in BONDS.items():
            try:
                close = data["Close"][info["ticker"]].dropna()
                val = close.iloc[-1] if len(close) > 0 else None
                result[key] = {"value": safe_round(val, 2), "name": info["name"]}
            except Exception:
                result[key] = {"value": None, "name": info["name"]}
    except Exception:
        result = {k: {"value": None, "name": v["name"]} for k, v in BONDS.items()}
    return result


def fetch_bonds():
    """Fetch bond yields. Only yfinance available (may not work from China)."""
    print("  Fetching from yfinance...")
    result = fetch_bonds_yfinance()
    return result


# ─── Main ───

def main():
    push = "--push" in sys.argv

    print("🚀 Kulono Market Data Updater")
    print("=" * 40)

    # Fetch all data
    print("\n📊 Fetching indices...")
    indices = fetch_all_indices()

    print("\n💱 Fetching forex...")
    forex, forex_source = fetch_forex()

    print("\n🪙 Fetching crypto...")
    crypto, crypto_source = fetch_crypto()

    print("\n📈 Fetching bond yields...")
    bonds = fetch_bonds()

    # Compose source string
    sources = []
    has_stooq = any(indices.get(k, {}).get("value") is not None for k in STOOQ_INDICES)
    if has_stooq:
        sources.append("Stooq")
    # Check if any yfinance data came through
    has_yf_indices = any(indices.get(k, {}).get("value") is not None for k in YFINANCE_INDICES)
    if has_yf_indices:
        sources.append("yfinance")
    if forex_source:
        sources.append(forex_source)
    if crypto_source:
        sources.append(crypto_source)
    source_str = " / ".join(sources) if sources else "N/A"

    # Build output
    cst = timezone(timedelta(hours=8))
    now = datetime.now(cst)

    # Merge all indices into a single dict
    all_indices = {}
    for key in ["SP500", "NASDAQ", "NIKKEI", "CSI300", "STOXX", "HSI"]:
        all_indices[key] = indices.get(key, {"value": None, "change": None, "name": key})

    output = {
        "last_updated": now.isoformat(),
        "source": source_str,
        "disclaimer": "Data sourced from public market feeds. May be delayed up to 15 minutes. For informational purposes only.",
        "indices": all_indices,
        "forex": forex,
        "crypto": crypto,
        "bonds": bonds,
    }

    # Write JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {OUTPUT_FILE} written (updated {now.strftime('%Y-%m-%d %H:%M:%S')} CST)")
    print(f"   Source: {source_str}")

    # Optional push
    if push:
        print("\n📤 Pushing to git...")
        try:
            subprocess.run(["git", "add", OUTPUT_FILE], check=True)
            subprocess.run([
                "git", "commit", "-m",
                f"Update market data {now.strftime('%Y-%m-%d %H:%M')}"
            ], check=True)
            subprocess.run(["git", "push", "origin", "main"], check=True)
            print("✅ Pushed to origin/main")
        except subprocess.CalledProcessError as e:
            print(f"❌ Git push failed: {e}")
            sys.exit(1)

    # Print summary
    print("\n📋 Summary:")
    for key, val in all_indices.items():
        v = val["value"]
        c = val["change"]
        v_str = f"{v:,.2f}" if v is not None else "N/A"
        c_str = f"{c:+.2f}%" if c is not None else "N/A"
        print(f"   {val['name']:20s}  {v_str:>12s}  {c_str}")
    print()
    for key, val in forex.items():
        v = val["value"]
        v_str = f"{v:.4f}" if v is not None else "N/A"
        print(f"   {val['name']:20s}  {v_str}")
    print()
    btc = crypto.get("btc_price")
    eth = crypto.get("eth_price")
    mcap = crypto.get("total_market_cap")
    dom = crypto.get("btc_dominance")
    print(f"   BTC Price:         {f'${btc:,.0f}' if btc else 'N/A'}")
    print(f"   ETH Price:         {f'${eth:,.0f}' if eth else 'N/A'}")
    print(f"   Total Market Cap:  {f'${mcap:,.0f}' if mcap else 'N/A'}")
    print(f"   BTC Dominance:     {f'{dom*100:.1f}%' if dom else 'N/A'}")
    print()
    for key, val in bonds.items():
        v = val["value"]
        v_str = f"{v:.2f}%" if v is not None else "N/A"
        print(f"   {val['name']:25s}  {v_str}")


if __name__ == "__main__":
    main()
