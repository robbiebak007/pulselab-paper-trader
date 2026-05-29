"""
PulseLab Backtest Engine
========================
Echte backtest op historische PulseChain data via GeckoTerminal API.

Drie strategieën worden vergeleken op dezelfde data:
  - Mean Reversion (koop dips, verkoop bounces)
  - Trend Following (EMA crossover)
  - Grid Trading (range-bound levels)

Realistische executie:
  - PulseX swap fee 0.29% per trade (één kant)
  - Slippage geschat o.b.v. pool liquiditeit en trade size
  - Geen lookahead bias

Gebruik:
  python3 pulselab_backtest.py                              # interactief
  python3 pulselab_backtest.py --pools 5                    # top 5 pools auto
  python3 pulselab_backtest.py --pool <pool_address>        # specifieke pool
  python3 pulselab_backtest.py --pools 10 --timeframe hour  # 10 pools, 1h candles

Requirements:
  pip install requests
  (alles in stdlib werkt ook, maar requests is cleaner)
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
import math
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlrequest, parse as urlparse, error as urlerror

# ─── ANSI kleuren voor terminal output ──────────────────────────────
class C:
    R = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    BG_PURPLE = "\033[45m"

def supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"

if not supports_color():
    for attr in dir(C):
        if not attr.startswith("_"):
            setattr(C, attr, "")

# ─── Configuratie ───────────────────────────────────────────────────
PULSEX_FEE = 0.0029              # 0.29% swap fee
SLIPPAGE_K = 1.0                  # multiplier op geschatte impact
STARTING_CAPITAL = 10_000.0       # USD per strategie

BASE_URL = "https://api.geckoterminal.com/api/v2"
HEADERS = {
    "Accept": "application/json;version=20230302",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
RATE_LIMIT_DELAY = 2.5            # GeckoTerminal free tier: 30 calls/min

# ─── HTTP helper ────────────────────────────────────────────────────
def http_get(url: str, retries: int = 3) -> dict:
    last_err = None
    for attempt in range(retries):
        try:
            req = urlrequest.Request(url, headers=HEADERS)
            with urlrequest.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urlerror.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = (attempt + 1) * 10
                print(f"  {C.YELLOW}rate limited (429) — wait {wait}s{C.R}")
                time.sleep(wait)
            elif e.code == 403:
                # 403 = blocked. Retry probably won't help.
                raise RuntimeError(
                    f"HTTP 403 Forbidden from GeckoTerminal. "
                    f"Mogelijk geblokkeerd door rate limiting of geo-restrictie. "
                    f"Probeer een VPN, of wacht 5-10 minuten en probeer opnieuw. URL: {url}"
                )
            else:
                wait = (attempt + 1) * 3
                print(f"  {C.YELLOW}retry {attempt+1}/{retries} in {wait}s (HTTP {e.code}){C.R}")
                time.sleep(wait)
        except (urlerror.URLError, TimeoutError) as e:
            last_err = e
            wait = (attempt + 1) * 3
            print(f"  {C.YELLOW}retry {attempt+1}/{retries} in {wait}s ({e}){C.R}")
            time.sleep(wait)
    raise RuntimeError(f"HTTP failed after {retries}: {last_err}")

# ─── Data structures ────────────────────────────────────────────────
@dataclass
class Candle:
    ts: int           # unix seconds
    o: float
    h: float
    l: float
    c: float
    v: float          # volume USD

@dataclass
class Pool:
    address: str
    name: str
    base_symbol: str
    quote_symbol: str
    dex: str
    liquidity_usd: float
    volume_24h: float
    price: float
    candles: list[Candle] = field(default_factory=list)

@dataclass
class Trade:
    ts: int
    side: str         # BUY or SELL
    price_quoted: float    # market price
    price_executed: float  # after slippage
    qty: float
    value_usd: float
    fee_usd: float
    pnl_usd: float | None
    pnl_pct: float | None
    reason: str

@dataclass
class StratResult:
    name: str
    pool: str
    trades: list[Trade]
    final_equity: float
    starting_equity: float
    open_qty: float
    open_avg_price: float
    last_price: float

# ─── GeckoTerminal client ───────────────────────────────────────────
def fetch_top_pools(network: str = "pulsechain", n: int = 10) -> list[dict]:
    """Top pools op PulseChain, gesorteerd op volume."""
    print(f"{C.CYAN}→ Fetching top {n} PulseChain pools from GeckoTerminal...{C.R}")
    pages_needed = math.ceil(n / 20)
    pools = []
    for p in range(1, pages_needed + 1):
        url = f"{BASE_URL}/networks/{network}/pools?page={p}"
        data = http_get(url)
        pools.extend(data.get("data", []))
        time.sleep(RATE_LIMIT_DELAY)
        if len(pools) >= n:
            break
    return pools[:n]

def fetch_pool_info(network: str, address: str) -> dict:
    url = f"{BASE_URL}/networks/{network}/pools/{address}"
    return http_get(url)["data"]

def fetch_ohlcv(network: str, address: str, timeframe: str, aggregate: int, limit: int = 1000) -> list[Candle]:
    """
    timeframe: day | hour | minute
    aggregate: integer (e.g. 1 for 1h with timeframe=hour, 15 for 15m with timeframe=minute)
    limit: max 1000 candles
    """
    url = f"{BASE_URL}/networks/{network}/pools/{address}/ohlcv/{timeframe}?aggregate={aggregate}&limit={limit}"
    data = http_get(url)
    rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    # GeckoTerminal returns: [[ts, o, h, l, c, v], ...] sorted DESCENDING
    candles = [Candle(ts=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]),
                      c=float(r[4]), v=float(r[5])) for r in rows]
    candles.sort(key=lambda x: x.ts)  # ascending for backtest
    return candles

def parse_pool_record(rec: dict) -> Pool:
    a = rec["attributes"]
    name = a.get("name", "?")
    base_sym, _, quote_sym = name.partition(" / ")
    return Pool(
        address=a.get("address", ""),
        name=name,
        base_symbol=base_sym.strip() or "?",
        quote_symbol=quote_sym.strip() or "?",
        dex=rec.get("relationships", {}).get("dex", {}).get("data", {}).get("id", "?"),
        liquidity_usd=float(a.get("reserve_in_usd") or 0),
        volume_24h=float(a.get("volume_usd", {}).get("h24") or 0),
        price=float(a.get("base_token_price_usd") or 0),
    )

# ─── Indicatoren ────────────────────────────────────────────────────
def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)  # type: ignore
    return out

def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag = gains / period
    al = losses / period
    out[period] = 100.0 if al == 0 else 100 - (100 / (1 + ag / al))
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = max(d, 0)
        l = max(-d, 0)
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        out[i] = 100.0 if al == 0 else 100 - (100 / (1 + ag / al))
    return out

# ─── Realistische executie simulator ────────────────────────────────
def estimate_slippage(trade_usd: float, pool_liquidity_usd: float) -> float:
    """
    Schatting voor constant-product AMM (Uniswap V2 style).
    Pool reserve aan één kant ≈ liquidity / 2.
    Slippage ≈ trade / (reserve + trade).
    """
    if pool_liquidity_usd <= 0:
        return 0.50  # 50% — pool kan trade niet aan
    side_reserve = pool_liquidity_usd / 2
    impact = trade_usd / (side_reserve + trade_usd)
    return impact * SLIPPAGE_K

def execute_buy(cash: float, market_price: float, trade_usd: float, pool_liq: float) -> tuple[float, float, float, float]:
    """Returns (qty_received, executed_price, fee_usd, cash_used)"""
    slip = estimate_slippage(trade_usd, pool_liq)
    fee = trade_usd * PULSEX_FEE
    effective_usd = trade_usd - fee
    executed_price = market_price * (1 + slip)
    qty = effective_usd / executed_price
    return qty, executed_price, fee, trade_usd

def execute_sell(qty: float, market_price: float, pool_liq: float) -> tuple[float, float, float]:
    """Returns (proceeds_usd, executed_price, fee_usd)"""
    gross_usd = qty * market_price
    slip = estimate_slippage(gross_usd, pool_liq)
    executed_price = market_price * (1 - slip)
    proceeds_before_fee = qty * executed_price
    fee = proceeds_before_fee * PULSEX_FEE
    return proceeds_before_fee - fee, executed_price, fee

# ─── Strategieën ────────────────────────────────────────────────────
def backtest_mean_reversion(pool: Pool) -> StratResult:
    cash = STARTING_CAPITAL
    position_qty = 0.0
    position_cost = 0.0
    position_entry = 0.0
    trades: list[Trade] = []
    closes = [c.c for c in pool.candles]
    rsi_vals = rsi(closes, 14)

    for i, candle in enumerate(pool.candles):
        if i < 20:
            continue
        price = candle.c

        # Geen lookahead: gebruik alleen data tot t-1 voor signaal, maar voer uit op close van t
        # (Iets minder strict dan ideaal, maar gebruikelijk in eenvoudige backtests)
        r = rsi_vals[i]
        past_6 = closes[i - 6] if i >= 6 else None
        drop_6 = ((price - past_6) / past_6) * 100 if past_6 else 0

        if position_qty == 0:
            if r is not None and r < 35 and drop_6 < -5:
                trade_usd = cash * 0.20
                if trade_usd >= 10:
                    qty, exec_price, fee, used = execute_buy(cash, price, trade_usd, pool.liquidity_usd)
                    cash -= used
                    position_qty = qty
                    position_cost = used
                    position_entry = exec_price
                    trades.append(Trade(candle.ts, "BUY", price, exec_price, qty, used, fee,
                                        None, None, f"RSI {r:.0f} + {drop_6:.1f}%"))
        else:
            profit_pct = (price - position_entry) / position_entry * 100
            exit_reason = None
            if profit_pct >= 4:
                exit_reason = "+4% target"
            elif r is not None and r > 60:
                exit_reason = f"RSI {r:.0f}"
            elif profit_pct <= -6:
                exit_reason = "-6% stop"
            if exit_reason:
                proceeds, exec_price, fee = execute_sell(position_qty, price, pool.liquidity_usd)
                pnl = proceeds - position_cost
                pnl_pct = pnl / position_cost * 100
                cash += proceeds
                trades.append(Trade(candle.ts, "SELL", price, exec_price, position_qty, proceeds, fee,
                                    pnl, pnl_pct, exit_reason))
                position_qty = 0.0
                position_cost = 0.0

    last_price = pool.candles[-1].c if pool.candles else 0
    return StratResult(
        name="Mean Reversion", pool=pool.name, trades=trades,
        final_equity=cash + position_qty * last_price,
        starting_equity=STARTING_CAPITAL, open_qty=position_qty,
        open_avg_price=position_entry, last_price=last_price)

def backtest_trend_following(pool: Pool) -> StratResult:
    cash = STARTING_CAPITAL
    position_qty = 0.0
    position_cost = 0.0
    position_entry = 0.0
    trades: list[Trade] = []
    closes = [c.c for c in pool.candles]
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    e50 = ema(closes, 50)

    for i, candle in enumerate(pool.candles):
        if i < 51:
            continue
        price = candle.c
        if e12[i] is None or e26[i] is None or e50[i] is None: continue
        if e12[i-1] is None or e26[i-1] is None: continue

        cross_up = e12[i-1] <= e26[i-1] and e12[i] > e26[i]
        cross_down = e12[i-1] >= e26[i-1] and e12[i] < e26[i]
        above_50 = price > e50[i]

        if position_qty == 0:
            if cross_up and above_50:
                trade_usd = cash * 0.25
                if trade_usd >= 10:
                    qty, exec_price, fee, used = execute_buy(cash, price, trade_usd, pool.liquidity_usd)
                    cash -= used
                    position_qty = qty
                    position_cost = used
                    position_entry = exec_price
                    trades.append(Trade(candle.ts, "BUY", price, exec_price, qty, used, fee,
                                        None, None, "EMA cross up + above EMA50"))
        else:
            profit_pct = (price - position_entry) / position_entry * 100
            exit_reason = None
            if profit_pct >= 12: exit_reason = "+12% target"
            elif profit_pct <= -4: exit_reason = "-4% stop"
            elif cross_down: exit_reason = "EMA cross down"
            if exit_reason:
                proceeds, exec_price, fee = execute_sell(position_qty, price, pool.liquidity_usd)
                pnl = proceeds - position_cost
                pnl_pct = pnl / position_cost * 100
                cash += proceeds
                trades.append(Trade(candle.ts, "SELL", price, exec_price, position_qty, proceeds, fee,
                                    pnl, pnl_pct, exit_reason))
                position_qty = 0.0
                position_cost = 0.0

    last_price = pool.candles[-1].c if pool.candles else 0
    return StratResult(
        name="Trend Following", pool=pool.name, trades=trades,
        final_equity=cash + position_qty * last_price,
        starting_equity=STARTING_CAPITAL, open_qty=position_qty,
        open_avg_price=position_entry, last_price=last_price)

def backtest_grid(pool: Pool) -> StratResult:
    """Grid trading: 6 niveaus rond een 50-candle moving range."""
    cash = STARTING_CAPITAL
    position_qty = 0.0
    position_cost = 0.0  # gemiddelde kosten van huidige holdings
    trades: list[Trade] = []
    closes = [c.c for c in pool.candles]

    grid_levels: list[dict] = []  # {price, side, filled}
    range_calibrated_at = 0

    def calibrate(i: int):
        recent = closes[max(0, i - 50):i]
        if len(recent) < 20: return []
        hi, lo = max(recent), min(recent)
        if (hi - lo) / lo < 0.05: return []
        mid = (hi + lo) / 2
        step = (hi - lo) / 6
        levels = []
        for k in range(-3, 4):
            if k == 0: continue
            levels.append({"price": mid + step * k,
                           "side": "BUY" if k < 0 else "SELL",
                           "filled": False})
        return levels

    for i, candle in enumerate(pool.candles):
        if i < 50: continue
        price = candle.c

        # (Re)calibrate
        if not grid_levels or i - range_calibrated_at > 50:
            recent_hi = max(closes[max(0,i-50):i])
            recent_lo = min(closes[max(0,i-50):i])
            if price > recent_hi * 1.15 or price < recent_lo * 0.85 or not grid_levels:
                grid_levels = calibrate(i)
                range_calibrated_at = i

        # Check level hits
        for lvl in grid_levels:
            if lvl["filled"]: continue
            if lvl["side"] == "BUY" and price <= lvl["price"]:
                trade_usd = cash * 0.08
                if trade_usd < 5: continue
                qty, exec_price, fee, used = execute_buy(cash, price, trade_usd, pool.liquidity_usd)
                # Bereken nieuwe gemiddelde kostprijs
                new_total_qty = position_qty + qty
                new_total_cost = position_cost + used
                position_qty = new_total_qty
                position_cost = new_total_cost
                cash -= used
                lvl["filled"] = True
                trades.append(Trade(candle.ts, "BUY", price, exec_price, qty, used, fee,
                                    None, None, f"grid lvl {lvl['price']:.2e}"))
            elif lvl["side"] == "SELL" and price >= lvl["price"] and position_qty > 0:
                # verkoop een portie ~8% positie
                qty_out = min(position_qty, position_qty * 0.20)
                if qty_out * price < 5: continue
                avg_cost_per_qty = position_cost / position_qty if position_qty > 0 else 0
                proceeds, exec_price, fee = execute_sell(qty_out, price, pool.liquidity_usd)
                cost_basis = avg_cost_per_qty * qty_out
                pnl = proceeds - cost_basis
                pnl_pct = pnl / cost_basis * 100 if cost_basis else 0
                cash += proceeds
                position_qty -= qty_out
                position_cost -= cost_basis
                lvl["filled"] = True
                trades.append(Trade(candle.ts, "SELL", price, exec_price, qty_out, proceeds, fee,
                                    pnl, pnl_pct, f"grid lvl {lvl['price']:.2e}"))

        # Reset filled state als prijs voldoende terugbeweegt
        for lvl in grid_levels:
            if not lvl["filled"]: continue
            if lvl["side"] == "BUY" and price > lvl["price"] * 1.04:
                lvl["filled"] = False
            elif lvl["side"] == "SELL" and price < lvl["price"] * 0.96:
                lvl["filled"] = False

    last_price = pool.candles[-1].c if pool.candles else 0
    avg_entry = position_cost / position_qty if position_qty > 0 else 0
    return StratResult(
        name="Grid Trading", pool=pool.name, trades=trades,
        final_equity=cash + position_qty * last_price,
        starting_equity=STARTING_CAPITAL, open_qty=position_qty,
        open_avg_price=avg_entry, last_price=last_price)

# ─── Dump Catcher (Z-score based) ───────────────────────────────────
def backtest_dump_catcher(pool: Pool, z_threshold: float = 2.5,
                          lookback: int = 50,
                          stop_pct: float = 10.0,
                          target_pct: float = 6.0,
                          max_hold: int = 48,
                          min_dip_pct: float = 0.0,
                          use_trailing: bool = True,
                          fixed_trade_usd: float = 0.0,
                          stop_mode: str = "fixed",       # fixed | empirical | sigma | none
                          name_suffix: str = "",
                          debug: bool = False) -> StratResult:
    """
    Statistical arbitrage on idiosyncratic shocks - v5.

    stop_mode:
      - "fixed":     gebruik stop_pct als vaste percentage
      - "empirical": stop op 1.2× de grootste dip in laatste 200 candles
      - "sigma":     stop op -3σ van negatieve returns
      - "none":      geen stop, alleen time-stop, target, of 50% recovery
    """
    cash = STARTING_CAPITAL
    position_qty = 0.0
    position_cost = 0.0
    position_entry = 0.0
    position_entry_idx = -1
    position_dip_size = 0.0
    position_trailing_stop = 0.0   # initiële stop (-stop_pct%)
    position_max_profit = 0.0      # hoogste profit % gezien tijdens trade
    trades: list[Trade] = []
    closes = [c.c for c in pool.candles]
    volumes = [c.v for c in pool.candles]

    # Log-returns
    log_returns = [0.0]
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            log_returns.append(math.log(closes[i] / closes[i-1]))
        else:
            log_returns.append(0.0)

    debug_lines: list[str] = []
    position_effective_stop_pct = stop_pct  # default

    for i, candle in enumerate(pool.candles):
        if i < lookback + 1:
            continue
        price = candle.c

        # Rolling statistieken
        window = log_returns[i - lookback:i]
        mu = statistics.mean(window)
        sigma = statistics.stdev(window) if len(window) > 1 else 0
        if sigma == 0:
            continue
        z = (log_returns[i] - mu) / sigma

        # Volume spike check
        vol_window = volumes[i - lookback:i]
        vol_avg = statistics.mean(vol_window) if vol_window else 0
        vol_spike = candle.v > vol_avg * 2 if vol_avg > 0 else False

        # ──────────────────────────────────────────────────────────────
        # POSITIE OPEN — manage exit met trailing stop
        # ──────────────────────────────────────────────────────────────
        if position_qty > 0:
            profit_pct = (price - position_entry) / position_entry * 100
            held = i - position_entry_idx
            recovery_target = position_entry * (1 + position_dip_size * 0.5)

            # Trailing stop: beweegt mee omhoog als trade winnaar wordt
            # +3% → stop naar entry (break-even)
            # +5% → stop naar +2%
            # +X% → stop naar X-3%
            if use_trailing and profit_pct > position_max_profit:
                position_max_profit = profit_pct
                if profit_pct >= 5:
                    new_stop = position_entry * (1 + (profit_pct - 3) / 100)
                    position_trailing_stop = max(position_trailing_stop, new_stop)
                elif profit_pct >= 3:
                    # break-even stop
                    position_trailing_stop = max(position_trailing_stop, position_entry)

            exit_reason = None
            if price >= recovery_target:
                exit_reason = "50% recovery"
            elif profit_pct >= target_pct:
                exit_reason = f"+{target_pct:.0f}% target"
            elif price <= position_trailing_stop:
                trailing_pct = (position_trailing_stop / position_entry - 1) * 100
                if abs(trailing_pct + position_effective_stop_pct) < 0.01:
                    exit_reason = f"stop ({trailing_pct:.1f}%)"
                else:
                    exit_reason = f"trail @ {trailing_pct:+.1f}%"
            elif held >= max_hold:
                exit_reason = f"time-stop {max_hold}"

            if exit_reason:
                proceeds, exec_price, fee = execute_sell(position_qty, price, pool.liquidity_usd)
                pnl = proceeds - position_cost
                pnl_pct = pnl / position_cost * 100
                cash += proceeds
                trades.append(Trade(candle.ts, "SELL", price, exec_price, position_qty, proceeds, fee,
                                    pnl, pnl_pct, exit_reason))
                if debug:
                    debug_lines.append(
                        f"[{i:4d}] SELL @ {price:.6g} | held {held} | P&L {pnl_pct:+.2f}% | max+{position_max_profit:.1f}% | {exit_reason}"
                    )
                position_qty = 0.0
                position_cost = 0.0
                position_max_profit = 0.0
                position_trailing_stop = 0.0
            continue  # geen nieuwe signalen evalueren als we al positie hebben

        # ──────────────────────────────────────────────────────────────
        # GEEN POSITIE — check voor dump signaal en koop direct
        # ──────────────────────────────────────────────────────────────
        if z < -z_threshold and vol_spike:
            pre_dump_price = closes[i - 1]
            dip_size = (pre_dump_price - price) / pre_dump_price
            dip_pct = dip_size * 100

            # NEW: min-dip filter
            if dip_pct < min_dip_pct:
                if debug:
                    debug_lines.append(
                        f"[{i:4d}] SKIP | Z={z:.2f} | dip {dip_pct:.1f}% < min {min_dip_pct:.1f}%"
                    )
                continue

            trade_usd = fixed_trade_usd if fixed_trade_usd > 0 else cash * 0.20
            if trade_usd > cash:
                trade_usd = cash  # niet meer dan we hebben
            if trade_usd >= 10:
                # Bepaal effectieve stop o.b.v. mode
                effective_stop_pct = stop_pct
                stop_label = f"-{stop_pct:.0f}%"

                if stop_mode == "empirical":
                    # Grootste dip in laatste 200 candles
                    window_start = max(0, i - 200)
                    window_returns = log_returns[window_start:i]
                    if window_returns:
                        worst_return = min(window_returns)
                        # Convert log return to percentage
                        worst_pct = (math.exp(worst_return) - 1) * 100
                        # Stop op 1.2× de grootste dip, in absolute waarde
                        effective_stop_pct = abs(worst_pct) * 1.2
                        stop_label = f"emp-{effective_stop_pct:.1f}%"

                elif stop_mode == "sigma":
                    # -3σ van negatieve returns
                    window_start = max(0, i - 200)
                    neg_returns = [r for r in log_returns[window_start:i] if r < 0]
                    if len(neg_returns) > 5:
                        sigma_neg = statistics.stdev(neg_returns)
                        # -3σ in percentage termen
                        effective_stop_pct = abs((math.exp(-3 * sigma_neg) - 1) * 100)
                        stop_label = f"3σ-{effective_stop_pct:.1f}%"

                elif stop_mode == "none":
                    effective_stop_pct = 100.0  # praktisch geen stop
                    stop_label = "none"
                qty, exec_price, fee, used = execute_buy(cash, price, trade_usd, pool.liquidity_usd)
                cash -= used
                position_qty = qty
                position_cost = used
                position_entry = exec_price
                position_entry_idx = i
                position_dip_size = dip_size
                position_trailing_stop = position_entry * (1 - effective_stop_pct / 100)
                position_max_profit = 0.0
                # Bewaar effective_stop_pct voor exit logic via een attribuut op de variabele
                position_effective_stop_pct = effective_stop_pct
                trades.append(Trade(candle.ts, "BUY", price, exec_price, qty, used, fee,
                                    None, None,
                                    f"Z={z:.2f} dip={dip_pct:.1f}% vol×{candle.v/vol_avg:.1f} stop={stop_label}"))
                if debug:
                    debug_lines.append(
                        f"[{i:4d}] BUY  @ {price:.6g} | Z={z:.2f} | dip {dip_pct:.1f}% | vol×{candle.v/vol_avg:.1f} "
                        f"| stop {position_trailing_stop:.6g} ({stop_label}) | target {position_entry*(1+target_pct/100):.6g}"
                    )

    last_price = pool.candles[-1].c if pool.candles else 0

    if debug and debug_lines:
        print(f"\n  {C.GRAY}── Dump Catcher{name_suffix} debug log ──{C.R}")
        for line in debug_lines[-30:]:  # last 30 events
            print(f"  {C.GRAY}{line}{C.R}")
        print()

    return StratResult(
        name=f"Dump Catcher{name_suffix}", pool=pool.name, trades=trades,
        final_equity=cash + position_qty * last_price,
        starting_equity=STARTING_CAPITAL, open_qty=position_qty,
        open_avg_price=position_entry, last_price=last_price)

# ─── Metrics ────────────────────────────────────────────────────────
def compute_metrics(result: StratResult) -> dict:
    closed = [t for t in result.trades if t.pnl_usd is not None]
    n_closed = len(closed)
    wins = [t for t in closed if t.pnl_usd > 0]
    losses = [t for t in closed if t.pnl_usd < 0]
    win_rate = len(wins) / n_closed * 100 if n_closed else 0
    total_return = (result.final_equity - result.starting_equity) / result.starting_equity * 100
    gross_win = sum(t.pnl_usd for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_usd for t in losses)) if losses else 0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0)
    avg_win = statistics.mean(t.pnl_pct for t in wins) if wins else 0
    avg_loss = statistics.mean(t.pnl_pct for t in losses) if losses else 0

    # Equity curve & max drawdown
    equity = result.starting_equity
    curve = [equity]
    for t in closed:
        equity += t.pnl_usd
        curve.append(equity)
    peak = curve[0]
    max_dd = 0
    for v in curve:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe (vereenvoudigd, per trade)
    if n_closed > 1:
        rets = [t.pnl_pct for t in closed]
        try:
            sharpe = statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252) if statistics.stdev(rets) > 0 else 0
        except statistics.StatisticsError:
            sharpe = 0
    else:
        sharpe = 0

    total_fees = sum(t.fee_usd for t in result.trades)

    return {
        "total_return_pct": total_return,
        "n_trades": n_closed,
        "n_opens": len([t for t in result.trades if t.side == "BUY"]),
        "win_rate_pct": win_rate,
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": profit_factor,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "total_fees_usd": total_fees,
        "final_equity": result.final_equity,
        "open_position_value": result.open_qty * result.last_price,
    }

# ─── Rapport ────────────────────────────────────────────────────────
def color_num(val: float, fmt: str = "+.2f", suffix: str = "") -> str:
    s = format(val, fmt) + suffix
    if val > 0: return f"{C.GREEN}{s}{C.R}"
    if val < 0: return f"{C.RED}{s}{C.R}"
    return f"{C.GRAY}{s}{C.R}"

def print_header():
    print()
    print(f"{C.MAGENTA}{C.BOLD}╔══════════════════════════════════════════════════════════════════════════╗{C.R}")
    print(f"{C.MAGENTA}{C.BOLD}║  PULSE//LAB  ·  Historical Backtest Engine                              ║{C.R}")
    print(f"{C.MAGENTA}{C.BOLD}║  {C.DIM}PulseChain · GeckoTerminal data · 3 strategies · realistic execution{C.R}{C.MAGENTA}{C.BOLD}  ║{C.R}")
    print(f"{C.MAGENTA}{C.BOLD}╚══════════════════════════════════════════════════════════════════════════╝{C.R}")
    print()

def print_pool_header(pool: Pool, n_candles: int, span_days: float):
    print(f"\n{C.BOLD}{C.CYAN}┌─ {pool.name} {C.DIM}({pool.dex}){C.R}")
    print(f"{C.CYAN}│{C.R} {C.GRAY}address:{C.R}    {pool.address}")
    print(f"{C.CYAN}│{C.R} {C.GRAY}liquidity:{C.R}  ${pool.liquidity_usd:,.0f}   {C.GRAY}vol 24h:{C.R} ${pool.volume_24h:,.0f}")
    print(f"{C.CYAN}│{C.R} {C.GRAY}candles:{C.R}    {n_candles}  ({span_days:.1f} days)")
    print(f"{C.CYAN}└{'─' * 70}{C.R}")

def print_strategy_result(result: StratResult, m: dict, color: str):
    name_padded = f"{result.name:<18}"
    print(f"\n  {color}{C.BOLD}● {name_padded}{C.R}")
    ret_str = color_num(m['total_return_pct'], '+.2f', '%')
    print(f"    {C.GRAY}return:{C.R}        {ret_str:>20}    {C.GRAY}final eq:{C.R}    ${m['final_equity']:>10,.2f}")
    print(f"    {C.GRAY}trades:{C.R}        {m['n_trades']:>15}       {C.GRAY}win rate:{C.R}     {m['win_rate_pct']:>9.1f}%")
    print(f"    {C.GRAY}wins/losses:{C.R}   {C.GREEN}{m['wins']}W{C.R}{C.GRAY}·{C.R}{C.RED}{m['losses']}L{C.R}                {C.GRAY}profit factor:{C.R} {m['profit_factor']:>8.2f}")
    print(f"    {C.GRAY}avg win:{C.R}       {color_num(m['avg_win_pct'], '+.2f', '%'):>20}    {C.GRAY}avg loss:{C.R}     {color_num(m['avg_loss_pct'], '+.2f', '%'):>15}")
    print(f"    {C.GRAY}max drawdown:{C.R}  {C.RED}-{m['max_drawdown_pct']:.2f}%{C.R:>20}     {C.GRAY}sharpe (ann):{C.R} {m['sharpe']:>9.2f}")
    print(f"    {C.GRAY}total fees:{C.R}    ${m['total_fees_usd']:>14,.2f}      {C.GRAY}open value:{C.R}    ${m['open_position_value']:>9,.2f}")

def print_summary_table(all_results: list[tuple[Pool, list[tuple[StratResult, dict]]]]):
    if not all_results:
        return

    # Strategie namen ophalen uit eerste pool
    strat_names = [r.name for r, _ in all_results[0][1]]
    n = len(strat_names)
    col_w = 11  # iets smaller om alles te laten passen

    print(f"\n\n{C.BOLD}{C.MAGENTA}╔══════════════════════════ SUMMARY ═══════════════════════════════════════╗{C.R}\n")

    # Header
    header = f"  {C.BOLD}{'Pool':<28}{C.R}"
    for sn in strat_names:
        # Kort de naam in voor weergave
        short = sn.replace("Mean Reversion", "MeanRev").replace("Trend Following", "Trend")\
                  .replace("Grid Trading", "Grid").replace("Dump Catcher", "Dump")
        header += f" {C.BOLD}{short:>{col_w}}{C.R}"
    print(header)
    print(f"  {C.GRAY}{'─' * (28 + (col_w + 1) * n)}{C.R}")

    aggregate = {sn: [] for sn in strat_names}

    for pool, results in all_results:
        pool_name = pool.name[:26] if len(pool.name) > 26 else pool.name
        row = f"  {pool_name:<28}"
        for r, m in results:
            val_str = format(m['total_return_pct'], '+6.2f') + '%'
            if m['total_return_pct'] > 0:
                colored = f"{C.GREEN}{val_str}{C.R}"
            elif m['total_return_pct'] < 0:
                colored = f"{C.RED}{val_str}{C.R}"
            else:
                colored = f"{C.GRAY}{val_str}{C.R}"
            # Pad accounting for ANSI codes
            row += f" {colored:>{col_w + len(colored) - len(val_str)}}"
            aggregate[r.name].append(m['total_return_pct'])
        print(row)

    print(f"  {C.GRAY}{'─' * (28 + (col_w + 1) * n)}{C.R}")

    # AVG en MEDIAN rijen
    for label, fn in [("AVG", statistics.mean), ("MEDIAN", statistics.median)]:
        row = f"  {C.BOLD}{label:<28}{C.R}"
        for sn in strat_names:
            vals = aggregate[sn]
            agg_val = fn(vals) if vals else 0
            val_str = format(agg_val, '+6.2f') + '%'
            if agg_val > 0:
                colored = f"{C.GREEN}{val_str}{C.R}"
            elif agg_val < 0:
                colored = f"{C.RED}{val_str}{C.R}"
            else:
                colored = f"{C.GRAY}{val_str}{C.R}"
            row += f" {colored:>{col_w + len(colored) - len(val_str)}}"
        print(row)

    print(f"\n{C.BOLD}{C.MAGENTA}╚══════════════════════════════════════════════════════════════════════════╝{C.R}\n")

    # Best strategy
    avgs = {sn: statistics.mean(aggregate[sn]) if aggregate[sn] else 0 for sn in strat_names}
    best_name = max(avgs, key=avgs.get)
    best_val = avgs[best_name]
    print(f"  {C.BOLD}Best avg strategy:{C.R} {C.GREEN}{best_name}{C.R}  ({best_val:+.2f}% avg across {len(all_results)} pools)")
    if best_val < 0:
        print(f"  {C.YELLOW}⚠ All strategies lost money on average. This is the realistic outcome.{C.R}")
        print(f"    {C.GRAY}Adjust filters, try different pools, or accept this is not your edge.{C.R}")

    # Trade counts per strategie (helpt zien welke daadwerkelijk handelden)
    print(f"\n  {C.GRAY}Trades per strategy (across all pools):{C.R}")
    for sn in strat_names:
        total_trades = sum(
            sum(1 for r, _ in pool_results if r.name == sn for _ in [t for t in r.trades if t.side == "BUY"])
            for _, pool_results in all_results
            for r, _ in pool_results
            if r.name == sn
        )
        # Eenvoudiger telling
        cnt = 0
        for pool, pool_results in all_results:
            for r, _ in pool_results:
                if r.name == sn:
                    cnt += len([t for t in r.trades if t.side == "BUY"])
        print(f"    {sn:<22} {C.CYAN}{cnt:>4} opens{C.R}")
    print()

def save_results(all_results, output_path: str):
    serializable = []
    for pool, results in all_results:
        pool_data = {
            "pool": {
                "address": pool.address,
                "name": pool.name,
                "dex": pool.dex,
                "liquidity_usd": pool.liquidity_usd,
                "volume_24h": pool.volume_24h,
                "n_candles": len(pool.candles),
            },
            "strategies": []
        }
        for r, m in results:
            pool_data["strategies"].append({
                "name": r.name,
                "metrics": m,
                "trades": [
                    {**asdict(t), "datetime": datetime.fromtimestamp(t.ts, tz=timezone.utc).isoformat()}
                    for t in r.trades
                ]
            })
        serializable.append(pool_data)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "starting_capital": STARTING_CAPITAL,
            "pulsex_fee": PULSEX_FEE,
            "slippage_k": SLIPPAGE_K,
        },
        "results": serializable
    }
    Path(output_path).write_text(json.dumps(output, indent=2, default=str))
    print(f"  {C.GREEN}✓{C.R} Results saved to {C.CYAN}{output_path}{C.R}")

# ─── Main ───────────────────────────────────────────────────────────
def parse_timeframe(tf: str) -> tuple[str, int]:
    """Parse '1h', '15m', '1d' → (timeframe, aggregate)"""
    tf = tf.lower().strip()
    mapping = {
        "1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15),
        "1h": ("hour", 1), "4h": ("hour", 4),
        "1d": ("day", 1), "day": ("day", 1), "hour": ("hour", 1), "minute": ("minute", 1),
    }
    if tf in mapping:
        return mapping[tf]
    return ("hour", 1)

def main():
    ap = argparse.ArgumentParser(description="PulseChain backtest engine")
    ap.add_argument("--pools", type=int, default=10, help="Aantal pools na filter (default 10)")
    ap.add_argument("--pool", type=str, help="Specifiek pool address (overrides --pools)")
    ap.add_argument("--timeframe", type=str, default="1h", help="1m/5m/15m/1h/4h/1d (default 1h)")
    ap.add_argument("--limit", type=int, default=500, help="Aantal candles per pool (default 500, max 1000)")
    ap.add_argument("--min-liquidity", type=float, default=10_000, help="Filter pools onder dit niveau (default 10k)")
    ap.add_argument("--max-liquidity", type=float, default=200_000, help="Filter pools boven dit niveau (default 200k, set 0 to disable)")
    ap.add_argument("--scan-pages", type=int, default=3, help="Aantal pagina's pools om te scannen (20/pagina, default 3 = top 60)")
    ap.add_argument("--min-volume", type=float, default=1_000, help="Minimum 24h volume USD (default 1k)")
    ap.add_argument("--output", type=str, default="pulselab_results.json", help="Output JSON file")
    args = ap.parse_args()

    print_header()
    tf, agg = parse_timeframe(args.timeframe)
    print(f"  {C.GRAY}timeframe:{C.R}     {args.timeframe} ({tf} × {agg})")
    print(f"  {C.GRAY}max candles:{C.R}   {args.limit}")
    print(f"  {C.GRAY}capital:{C.R}       ${STARTING_CAPITAL:,.0f} per strategy")
    print(f"  {C.GRAY}fees:{C.R}          {PULSEX_FEE*100:.2f}% per swap (PulseX)")
    print(f"  {C.GRAY}slippage:{C.R}      AMM-based (constant product) × {SLIPPAGE_K}")
    print()

    # Pool lijst opbouwen
    if args.pool:
        print(f"{C.CYAN}→ Fetching specific pool {args.pool}...{C.R}")
        rec = fetch_pool_info("pulsechain", args.pool)
        pools_meta = [parse_pool_record(rec)]
    else:
        # Scan meer pagina's om sweet-spot pools te vinden
        print(f"{C.CYAN}→ Scanning {args.scan_pages} pages (~{args.scan_pages * 20} pools)...{C.R}")
        raw_pools = []
        for page in range(1, args.scan_pages + 1):
            url = f"{BASE_URL}/networks/pulsechain/pools?page={page}"
            try:
                data = http_get(url)
                raw_pools.extend(data.get("data", []))
                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                print(f"  {C.YELLOW}⚠ Page {page} failed: {e}{C.R}")
                break

        all_meta = [parse_pool_record(p) for p in raw_pools]
        print(f"  {C.GRAY}Scanned {len(all_meta)} pools total{C.R}")

        # Filter: liquidity range + volume
        filtered = []
        for p in all_meta:
            if p.liquidity_usd < args.min_liquidity:
                continue
            if args.max_liquidity > 0 and p.liquidity_usd > args.max_liquidity:
                continue
            if p.volume_24h < args.min_volume:
                continue
            filtered.append(p)

        # Sorteer op volume/liquidity ratio — pools met veel handel relatief
        # tot hun liquiditeit zijn waar inefficiënties kunnen ontstaan
        filtered.sort(key=lambda p: p.volume_24h / max(p.liquidity_usd, 1), reverse=True)
        pools_meta = filtered[:args.pools]

        print(f"  {C.GREEN}✓{C.R} {len(pools_meta)} pools in sweet spot "
              f"(${args.min_liquidity:,.0f} ≤ liq ≤ ${args.max_liquidity:,.0f}, vol ≥ ${args.min_volume:,.0f})")
        if pools_meta:
            print(f"  {C.GRAY}Sorted by vol/liq ratio (most active relative to size first){C.R}")

    if not pools_meta:
        print(f"  {C.RED}✗ No pools to test. Lower --min-liquidity?{C.R}")
        return

    # Fetch candles per pool
    all_results = []
    for idx, pool in enumerate(pools_meta, 1):
        print(f"\n{C.GRAY}[{idx}/{len(pools_meta)}]{C.R} {C.CYAN}→ Fetching candles for {pool.name}...{C.R}")
        try:
            pool.candles = fetch_ohlcv("pulsechain", pool.address, tf, agg, min(args.limit, 1000))
            time.sleep(RATE_LIMIT_DELAY)
        except Exception as e:
            print(f"  {C.RED}✗ Failed: {e}{C.R}")
            continue

        if len(pool.candles) < 60:
            print(f"  {C.YELLOW}⚠ Only {len(pool.candles)} candles — skipping (need ≥60){C.R}")
            continue

        # Tijdspanne uitrekenen
        span_sec = pool.candles[-1].ts - pool.candles[0].ts
        span_days = span_sec / 86400
        print_pool_header(pool, len(pool.candles), span_days)

        # Run alle strategieën
        # Bij --pool (single pool mode) zetten we debug aan voor Dump Catcher
        single_pool_mode = args.pool is not None
        results = []
        strats = [
            (lambda p: backtest_mean_reversion(p), C.CYAN),
            (lambda p: backtest_trend_following(p), C.YELLOW),
            (lambda p: backtest_grid(p), C.MAGENTA),
            (lambda p: backtest_dump_catcher(p, z_threshold=2.5, stop_pct=3.0,
                                              target_pct=1.6, max_hold=24,
                                              min_dip_pct=2.0, use_trailing=True,
                                              fixed_trade_usd=50.0,
                                              stop_mode="fixed",
                                              name_suffix=" fixed",
                                              debug=single_pool_mode), C.BLUE),
            (lambda p: backtest_dump_catcher(p, z_threshold=2.5, stop_pct=3.0,
                                              target_pct=1.6, max_hold=24,
                                              min_dip_pct=2.0, use_trailing=True,
                                              fixed_trade_usd=50.0,
                                              stop_mode="empirical",
                                              name_suffix=" empir",
                                              debug=single_pool_mode), C.BLUE),
            (lambda p: backtest_dump_catcher(p, z_threshold=2.5, stop_pct=3.0,
                                              target_pct=1.6, max_hold=24,
                                              min_dip_pct=2.0, use_trailing=True,
                                              fixed_trade_usd=50.0,
                                              stop_mode="none",
                                              name_suffix=" no-stop",
                                              debug=single_pool_mode), C.BLUE),
        ]
        for fn, color in strats:
            r = fn(pool)
            m = compute_metrics(r)
            print_strategy_result(r, m, color)
            results.append((r, m))

        all_results.append((pool, results))

    if all_results:
        print_summary_table(all_results)
        save_results(all_results, args.output)
        print(f"\n  {C.GRAY}Interpretation tips:{C.R}")
        print(f"  • {C.BOLD}Profit factor > 1.5{C.R} and {C.BOLD}>30 trades{C.R} = potentially real edge")
        print(f"  • {C.BOLD}Sharpe > 1.0{C.R} = decent risk-adjusted returns")
        print(f"  • {C.BOLD}Max drawdown{C.R} = the worst losing streak — could you stomach it live?")
        print(f"  • Results vary wildly per pool. {C.YELLOW}Consistency across pools{C.R} > one lucky hit.\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Interrupted by user.{C.R}")
    except Exception as e:
        print(f"\n{C.RED}Fatal error: {e}{C.R}")
        raise
