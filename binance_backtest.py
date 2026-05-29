"""
PulseLab Backtest — Binance edition
====================================
Test de Dump Catcher strategie op echte CEX data:
  - BTC/USDT (of andere paar) op 1m candles
  - Tot 30 dagen historie = ~43.200 candles
  - Gratis Binance API, geen key nodig
  - Multi-call data loading (Binance max 1000 per call)

Gebruik:
  python3 binance_backtest.py                              # BTC/USDT 1m 30 dagen
  python3 binance_backtest.py --symbol ETHUSDT             # andere pair
  python3 binance_backtest.py --days 90                    # langere periode
  python3 binance_backtest.py --interval 5m --days 90      # 5m candles, 90 dagen

Geen dependencies. Pure Python stdlib.
"""

from __future__ import annotations
import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib import request as urlrequest, error as urlerror

# ─── ANSI kleuren ───────────────────────────────────────────────────
class C:
    R = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"
    BLUE = "\033[34m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"; GRAY = "\033[90m"

if not (sys.stdout.isatty() and os.environ.get("TERM") != "dumb"):
    for attr in dir(C):
        if not attr.startswith("_"): setattr(C, attr, "")

# ─── Config ────────────────────────────────────────────────────────
BINANCE_FEE = 0.001               # 0.1% taker fee per kant
SLIPPAGE_K = 0.5                  # CEX is veel beter dan AMM, lagere slippage
STARTING_CAPITAL = 10_000.0

BASE_URL = "https://api.binance.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PulseLabBacktest/1.0)",
}

# ─── HTTP ──────────────────────────────────────────────────────────
def http_get(url: str, retries: int = 3) -> Any:
    for attempt in range(retries):
        try:
            req = urlrequest.Request(url, headers=HEADERS)
            with urlrequest.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urlerror.HTTPError as e:
            if e.code == 429 or e.code == 418:
                wait = 30
                print(f"  {C.YELLOW}rate limited ({e.code}) — wait {wait}s{C.R}")
                time.sleep(wait)
            elif e.code == 403:
                raise RuntimeError(f"403 Forbidden from Binance: {url}")
            else:
                wait = (attempt + 1) * 3
                print(f"  {C.YELLOW}retry {attempt+1}/{retries} in {wait}s (HTTP {e.code}){C.R}")
                time.sleep(wait)
        except (urlerror.URLError, TimeoutError) as e:
            wait = (attempt + 1) * 3
            print(f"  {C.YELLOW}retry {attempt+1}/{retries} ({e}){C.R}")
            time.sleep(wait)
    raise RuntimeError("HTTP failed")

# ─── Data ──────────────────────────────────────────────────────────
@dataclass
class Candle:
    ts: int
    o: float; h: float; l: float; c: float
    v: float

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    """
    Fetch klines van Binance. Auto-paginates (Binance limit = 1000 per call).
    """
    all_candles: list[Candle] = []
    current_start = start_ms

    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }.get(interval, 60_000)

    total_expected = (end_ms - start_ms) // interval_ms
    call_count = 0
    print(f"  {C.GRAY}Expected ~{total_expected:,} candles total{C.R}")

    while current_start < end_ms:
        url = (f"{BASE_URL}/api/v3/klines?symbol={symbol}"
               f"&interval={interval}&startTime={current_start}"
               f"&endTime={end_ms}&limit=1000")
        data = http_get(url)
        if not data:
            break
        call_count += 1

        for k in data:
            all_candles.append(Candle(
                ts=int(k[0]),
                o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]),
                v=float(k[5]),
            ))

        last_ts = int(data[-1][0])
        if last_ts >= end_ms - interval_ms:
            break
        current_start = last_ts + interval_ms

        # Progress
        progress = (current_start - start_ms) / (end_ms - start_ms) * 100
        print(f"  {C.GRAY}  call {call_count}: {len(all_candles):,} candles loaded ({progress:.0f}%){C.R}",
              end="\r")

        time.sleep(0.2)  # rate limit voorzichtigheid

    print(f"  {C.GREEN}✓{C.R} Loaded {len(all_candles):,} candles in {call_count} calls{' ' * 30}")
    return all_candles

# ─── Strategy: Dump Catcher (zelfde logica als PulseChain versie) ──
@dataclass
class Trade:
    ts: int
    side: str
    price: float
    qty: float
    value_usd: float
    fee_usd: float
    pnl_usd: float | None
    pnl_pct: float | None
    reason: str

def estimate_slippage(trade_usd: float, candle_volume_usd: float) -> float:
    """
    Op CEX is slippage veel kleiner. We benaderen:
      - kleine trades (<0.1% van 1m volume) → 0.01% slippage
      - grote trades → schaalt op met grootte/volume
    """
    if candle_volume_usd <= 0:
        return 0.001
    ratio = trade_usd / candle_volume_usd
    return min(ratio * 0.5 * SLIPPAGE_K, 0.02)  # max 2%

def execute_buy(price: float, trade_usd: float, vol_usd: float):
    slip = estimate_slippage(trade_usd, vol_usd)
    fee = trade_usd * BINANCE_FEE
    effective = trade_usd - fee
    exec_price = price * (1 + slip)
    qty = effective / exec_price
    return qty, exec_price, fee, trade_usd

def execute_sell(qty: float, price: float, vol_usd: float):
    gross_usd = qty * price
    slip = estimate_slippage(gross_usd, vol_usd)
    exec_price = price * (1 - slip)
    proceeds_before = qty * exec_price
    fee = proceeds_before * BINANCE_FEE
    return proceeds_before - fee, exec_price, fee

def backtest_dump_catcher(candles: list[Candle], symbol: str,
                          z_threshold: float = 2.5,
                          lookback: int = 50,
                          stop_pct: float = 10.0,
                          target_pct: float = 6.0,
                          max_hold: int = 48,
                          min_dip_pct: float = 0.5,
                          use_trailing: bool = True,
                          fixed_trade_usd: float = 200.0,
                          stop_mode: str = "none",
                          sigma_mode: str = "rolling",   # rolling | warmup | expanding
                          warmup_idx: int = 0,            # geen trades vóór deze index
                          name: str = "Dump Catcher") -> dict:
    cash = STARTING_CAPITAL
    position_qty = 0.0
    position_cost = 0.0
    position_entry = 0.0
    position_entry_idx = -1
    position_dip_size = 0.0
    position_trailing_stop = 0.0
    position_max_profit = 0.0
    position_effective_stop_pct = stop_pct
    trades: list[Trade] = []

    closes = [c.c for c in candles]
    log_returns = [0.0]
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            log_returns.append(math.log(closes[i] / closes[i-1]))
        else:
            log_returns.append(0.0)

    # ── Statistics computation: GEEN LOOKAHEAD ─────────────────────
    # sigma_mode "warmup": σ wordt 1x berekend over [0:warmup_idx]
    # sigma_mode "expanding": σ groeit met elke candle in test-periode
    # sigma_mode "rolling": σ over laatste `lookback` candles (klassiek)
    warmup_mu = 0.0
    warmup_sigma = 0.0
    if warmup_idx > 50:
        warmup_returns = log_returns[1:warmup_idx]  # skip 0
        if len(warmup_returns) > 1:
            warmup_mu = statistics.mean(warmup_returns)
            warmup_sigma = statistics.stdev(warmup_returns)

    # DEBUG: print actual sigma values voor de gekozen mode
    sigma_samples: list[float] = []  # voor verificatie

    # Pre-compute expanding mean/variance for performance (Welford's online algorithm)
    # For sigma_mode="expanding": at index i, we have mean/std of log_returns[1:i]
    expanding_mean = [0.0] * len(log_returns)
    expanding_var = [0.0] * len(log_returns)  # variance
    if sigma_mode == "expanding":
        running_mean = 0.0
        running_M2 = 0.0  # sum of squared deviations
        n = 0
        for k in range(1, len(log_returns)):
            n += 1
            x = log_returns[k]
            delta = x - running_mean
            running_mean += delta / n
            delta2 = x - running_mean
            running_M2 += delta * delta2
            expanding_mean[k] = running_mean
            expanding_var[k] = running_M2 / (n - 1) if n > 1 else 0

    WIDE_WINDOW = 500

    skipped_min_dip = 0
    skipped_no_vol = 0

    for i, candle in enumerate(candles):
        if i < lookback + 1: continue
        # Skip warmup periode — daar geen trades, alleen σ-opbouw
        if i < warmup_idx: continue
        price = candle.c

        # Z-score berekening op basis van sigma_mode
        if sigma_mode == "warmup":
            # Gebruik σ uit warmup-periode (vast voor hele test)
            mu = warmup_mu
            sigma = warmup_sigma
        elif sigma_mode == "expanding":
            # σ over alles van begin tot voor candle i (pre-computed)
            mu = expanding_mean[i - 1] if i > 0 else 0
            var = expanding_var[i - 1] if i > 0 else 0
            sigma = math.sqrt(var) if var > 0 else 0
        else:  # "rolling" — klassiek
            window = log_returns[i - lookback:i]
            mu = statistics.mean(window)
            sigma = statistics.stdev(window) if len(window) > 1 else 0

        if sigma == 0: continue
        z = (log_returns[i] - mu) / sigma

        # Sample sigma every 1000 candles for diagnostic
        if i % 1000 == 0:
            sigma_samples.append(sigma)

        # Volume spike check
        vol_window = [c.v for c in candles[i - lookback:i]]
        vol_avg = statistics.mean(vol_window) if vol_window else 0
        vol_spike = candle.v > vol_avg * 2 if vol_avg > 0 else False

        # ── Exit logic ──
        if position_qty > 0:
            profit_pct = (price - position_entry) / position_entry * 100
            held = i - position_entry_idx
            recovery_target = position_entry * (1 + position_dip_size * 0.5)

            if use_trailing and profit_pct > position_max_profit:
                position_max_profit = profit_pct
                if profit_pct >= 5:
                    new_stop = position_entry * (1 + (profit_pct - 3) / 100)
                    position_trailing_stop = max(position_trailing_stop, new_stop)
                elif profit_pct >= 3:
                    position_trailing_stop = max(position_trailing_stop, position_entry)

            exit_reason = None
            if price >= recovery_target:
                exit_reason = "50% recovery"
            elif profit_pct >= target_pct:
                exit_reason = f"+{target_pct:.1f}% target"
            elif price <= position_trailing_stop and stop_mode != "none":
                trailing_pct = (position_trailing_stop / position_entry - 1) * 100
                exit_reason = f"stop {trailing_pct:+.1f}%"
            elif held >= max_hold:
                exit_reason = f"time {max_hold}"

            if exit_reason:
                vol_usd = candle.v * candle.c
                proceeds, exec_price, fee = execute_sell(position_qty, price, vol_usd)
                pnl = proceeds - position_cost
                pnl_pct = pnl / position_cost * 100
                cash += proceeds
                trades.append(Trade(candle.ts, "SELL", exec_price, position_qty,
                                    proceeds, fee, pnl, pnl_pct, exit_reason))
                position_qty = 0.0; position_cost = 0.0
                position_max_profit = 0.0; position_trailing_stop = 0.0
            continue

        # ── Entry logic ──
        if z < -z_threshold and vol_spike:
            pre_dump_price = closes[i - 1]
            dip_size = (pre_dump_price - price) / pre_dump_price
            dip_pct = dip_size * 100

            if dip_pct < min_dip_pct:
                skipped_min_dip += 1
                continue

            vol_usd = candle.v * candle.c
            if vol_usd < 1000:  # skip dead candles
                skipped_no_vol += 1
                continue

            trade_usd = fixed_trade_usd if fixed_trade_usd > 0 else cash * 0.20
            trade_usd = min(trade_usd, cash)
            if trade_usd < 10: continue

            # Effective stop
            if stop_mode == "fixed":
                effective_stop = stop_pct
            elif stop_mode == "empirical":
                w200 = log_returns[max(0, i-200):i]
                if w200:
                    worst = min(w200)
                    effective_stop = abs((math.exp(worst) - 1) * 100) * 1.2
                else:
                    effective_stop = stop_pct
            elif stop_mode == "sigma":
                neg = [r for r in log_returns[max(0, i-200):i] if r < 0]
                if len(neg) > 5:
                    s = statistics.stdev(neg)
                    effective_stop = abs((math.exp(-3 * s) - 1) * 100)
                else:
                    effective_stop = stop_pct
            else:  # "none"
                effective_stop = 100.0

            position_effective_stop_pct = effective_stop

            qty, exec_price, fee, used = execute_buy(price, trade_usd, vol_usd)
            cash -= used
            position_qty = qty
            position_cost = used
            position_entry = exec_price
            position_entry_idx = i
            position_dip_size = dip_size
            position_trailing_stop = position_entry * (1 - effective_stop / 100)
            position_max_profit = 0.0
            trades.append(Trade(candle.ts, "BUY", exec_price, qty, used, fee,
                                None, None,
                                f"Z={z:.2f} dip={dip_pct:.2f}%"))

    last_price = candles[-1].c if candles else 0
    final_eq = cash + position_qty * last_price

    avg_sigma = statistics.mean(sigma_samples) if sigma_samples else 0
    sigma_range = (min(sigma_samples), max(sigma_samples)) if sigma_samples else (0, 0)

    return {
        "name": name,
        "symbol": symbol,
        "trades": trades,
        "final_equity": final_eq,
        "starting_equity": STARTING_CAPITAL,
        "open_qty": position_qty,
        "last_price": last_price,
        "skipped_min_dip": skipped_min_dip,
        "skipped_no_vol": skipped_no_vol,
        "sigma_mode": sigma_mode,
        "avg_sigma": avg_sigma,
        "sigma_min": sigma_range[0],
        "sigma_max": sigma_range[1],
    }

# ─── Metrics & rendering ────────────────────────────────────────────
def compute_metrics(result: dict) -> dict:
    trades = result["trades"]
    closed = [t for t in trades if t.pnl_usd is not None]
    wins = [t for t in closed if t.pnl_usd > 0]
    losses = [t for t in closed if t.pnl_usd < 0]
    n = len(closed)
    win_rate = len(wins) / n * 100 if n else 0
    total_ret = (result["final_equity"] - result["starting_equity"]) / result["starting_equity"] * 100
    gw = sum(t.pnl_usd for t in wins) if wins else 0
    gl = abs(sum(t.pnl_usd for t in losses)) if losses else 0
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0)
    avg_w = statistics.mean([t.pnl_pct for t in wins]) if wins else 0
    avg_l = statistics.mean([t.pnl_pct for t in losses]) if losses else 0

    # Equity curve & drawdown
    eq = result["starting_equity"]
    curve = [eq]
    for t in closed:
        eq += t.pnl_usd; curve.append(eq)
    peak = curve[0]; mdd = 0
    for v in curve:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        mdd = max(mdd, dd)

    if n > 1:
        rets = [t.pnl_pct for t in closed]
        try:
            std = statistics.stdev(rets)
            sharpe = statistics.mean(rets) / std * math.sqrt(252 * 24 * 60) if std > 0 else 0
            # sqrt(252 * 24 * 60) voor 1m candles... eigenlijk te hoog. Better:
            # gebruik per-trade Sharpe zonder annualisering
            sharpe_trade = statistics.mean(rets) / std if std > 0 else 0
        except Exception:
            sharpe = 0; sharpe_trade = 0
    else:
        sharpe = 0; sharpe_trade = 0

    return {
        "total_return_pct": total_ret,
        "n_trades": n,
        "n_opens": len([t for t in trades if t.side == "BUY"]),
        "win_rate_pct": win_rate,
        "wins": len(wins), "losses": len(losses),
        "profit_factor": pf,
        "avg_win_pct": avg_w, "avg_loss_pct": avg_l,
        "max_drawdown_pct": mdd,
        "sharpe_per_trade": sharpe_trade,
        "total_fees": sum(t.fee_usd for t in trades),
        "final_equity": result["final_equity"],
    }

def color_pct(val: float) -> str:
    s = f"{val:+.2f}%"
    if val > 0: return f"{C.GREEN}{s}{C.R}"
    if val < 0: return f"{C.RED}{s}{C.R}"
    return f"{C.GRAY}{s}{C.R}"

def print_result(result: dict, m: dict, color: str = C.CYAN):
    print(f"\n{color}{C.BOLD}● {result['name']}{C.R}")
    print(f"  {C.GRAY}symbol:{C.R}        {result['symbol']}")
    sigma_info = f"avg σ={result.get('avg_sigma',0):.6f} (range {result.get('sigma_min',0):.6f}-{result.get('sigma_max',0):.6f})"
    print(f"  {C.GRAY}σ-mode:{C.R}        {result.get('sigma_mode','?')}  {C.DIM}{sigma_info}{C.R}")
    print(f"  {C.GRAY}return:{C.R}        {color_pct(m['total_return_pct']):>15}   {C.GRAY}final eq:{C.R}    ${m['final_equity']:>10,.2f}")
    print(f"  {C.GRAY}trades:{C.R}        {m['n_trades']:>10}        {C.GRAY}win rate:{C.R}     {m['win_rate_pct']:>9.1f}%")
    print(f"  {C.GRAY}W/L:{C.R}           {C.GREEN}{m['wins']}W{C.R}·{C.RED}{m['losses']}L{C.R}{' '*7}       {C.GRAY}profit factor:{C.R} {m['profit_factor']:>8.2f}")
    print(f"  {C.GRAY}avg win:{C.R}       {color_pct(m['avg_win_pct']):>15}   {C.GRAY}avg loss:{C.R}     {color_pct(m['avg_loss_pct']):>10}")
    print(f"  {C.GRAY}max drawdown:{C.R}  {C.RED}-{m['max_drawdown_pct']:.2f}%{C.R:>10}    {C.GRAY}sharpe/trade:{C.R} {m['sharpe_per_trade']:>9.3f}")
    print(f"  {C.GRAY}total fees:{C.R}    ${m['total_fees']:>10,.2f}    {C.GRAY}skipped:{C.R} min-dip={result.get('skipped_min_dip',0)}, vol={result.get('skipped_no_vol',0)}")

def print_recent_trades(trades: list[Trade], n: int = 20):
    print(f"\n  {C.GRAY}── Last {min(n, len(trades))} trade events ──{C.R}")
    for t in trades[-n:]:
        time_str = datetime.fromtimestamp(t.ts/1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        side_color = C.GREEN if t.side == "BUY" else C.RED
        pnl_str = ""
        if t.pnl_pct is not None:
            pnl_str = f" P&L {color_pct(t.pnl_pct)}"
        print(f"  {C.GRAY}{time_str}{C.R}  {side_color}{t.side:<4}{C.R} @ {t.price:>10,.4f}  {C.GRAY}|{C.R} {t.reason}{pnl_str}")

# ─── Main ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT,ETHUSDT,SOLUSDT",
                    help="Comma-separated Binance symbols (default: BTC,ETH,SOL)")
    ap.add_argument("--interval", default="1m", help="1m/3m/5m/15m/30m/1h/4h/1d (default 1m)")
    ap.add_argument("--days", type=int, default=60, help="Totaal aantal dagen historie (default 60)")
    ap.add_argument("--warmup-days", type=int, default=30, help="Dagen voor warmup/σ-berekening (default 30)")
    ap.add_argument("--output", default="binance_results.json")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]

    print()
    print(f"{C.MAGENTA}{C.BOLD}╔══════════════════════════════════════════════════════════════════════╗{C.R}")
    print(f"{C.MAGENTA}{C.BOLD}║  PULSE//LAB · BINANCE EDITION                                       ║{C.R}")
    print(f"{C.MAGENTA}{C.BOLD}║  {C.DIM}Dump Catcher cross-symbol validatie · echte CEX data{C.R}{C.MAGENTA}{C.BOLD}              ║{C.R}")
    print(f"{C.MAGENTA}{C.BOLD}╚══════════════════════════════════════════════════════════════════════╝{C.R}")
    print()
    print(f"  {C.GRAY}symbols:{C.R}      {', '.join(symbols)}")
    print(f"  {C.GRAY}interval:{C.R}     {args.interval}")
    print(f"  {C.GRAY}total days:{C.R}   {args.days} (warmup {args.warmup_days} + test {args.days - args.warmup_days})")
    print(f"  {C.GRAY}capital:{C.R}      ${STARTING_CAPITAL:,.0f}")
    print(f"  {C.GRAY}fee:{C.R}          {BINANCE_FEE*100:.2f}% per side")
    print(f"  {C.GRAY}lookahead:{C.R}    NO — train/test split for clean validation")
    print()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86400 * 1000

    variants = [
        {"sigma_mode": "rolling", "z": 2.5, "target": 3.0, "min_dip": 1.0,
         "name": "rolling-50"},
        {"sigma_mode": "warmup", "z": 2.5, "target": 3.0, "min_dip": 1.0,
         "name": "warmup-σ"},
        {"sigma_mode": "expanding", "z": 2.5, "target": 3.0, "min_dip": 1.0,
         "name": "expanding-σ"},
    ]
    colors = [C.CYAN, C.BLUE, C.MAGENTA]

    # results[symbol] = [(result, metrics), ...]
    all_results: dict[str, list] = {}

    for symbol in symbols:
        print(f"\n{C.YELLOW}{C.BOLD}═══ {symbol} ═══{C.R}")
        print(f"{C.CYAN}→ Fetching {symbol} {args.interval} candles...{C.R}")
        try:
            candles = fetch_klines(symbol, args.interval, start_ms, end_ms)
        except Exception as e:
            print(f"  {C.RED}✗ Failed: {e}{C.R}")
            continue

        if len(candles) < 100:
            print(f"  {C.RED}✗ Too few candles, skipping{C.R}")
            continue

        span_days = (candles[-1].ts - candles[0].ts) / 86400000
        # Bereken warmup_idx: de eerste warmup-dagen zijn alleen voor σ
        warmup_ms = args.warmup_days * 86400 * 1000
        warmup_cutoff_ts = candles[0].ts + warmup_ms
        warmup_idx = 0
        for j, c in enumerate(candles):
            if c.ts >= warmup_cutoff_ts:
                warmup_idx = j
                break
        test_days = span_days - args.warmup_days
        print(f"  {C.GRAY}coverage: {span_days:.1f} days total{C.R}")
        print(f"  {C.GRAY}  warmup: {args.warmup_days} days ({warmup_idx} candles) — no trades, σ-buildup{C.R}")
        print(f"  {C.GRAY}  test:   {test_days:.1f} days ({len(candles) - warmup_idx} candles){C.R}")

        sym_results = []
        for v, col in zip(variants, colors):
            r = backtest_dump_catcher(
                candles, symbol,
                z_threshold=v["z"], target_pct=v["target"],
                min_dip_pct=v["min_dip"],
                stop_mode="none",
                sigma_mode=v["sigma_mode"],
                warmup_idx=warmup_idx,
                max_hold=180, name=f"{symbol} {v['name']}",
            )
            m = compute_metrics(r)
            print_result(r, m, col)
            sym_results.append((r, m))

        all_results[symbol] = sym_results

    if not all_results:
        print(f"{C.RED}✗ No results.{C.R}")
        return

    # ─── CROSS-SYMBOL SUMMARY ──────────────────────────────────────
    print(f"\n\n{C.BOLD}{C.MAGENTA}╔══════════════════ CROSS-SYMBOL SUMMARY ════════════════════════════════╗{C.R}\n")

    # Per-symbol breakdown
    print(f"  {C.BOLD}{'Symbol':<10} {'Variant':<14} {'Return':>10} {'Trades':>8} {'WinRate':>9} {'PF':>7} {'MDD':>8}{C.R}")
    print(f"  {C.GRAY}{'─' * 75}{C.R}")
    for symbol, sym_results in all_results.items():
        for r, m in sym_results:
            short_name = r["name"].replace(symbol + " ", "")
            ret_str = f"{m['total_return_pct']:+.2f}%"
            ret_color = C.GREEN if m['total_return_pct'] > 0 else C.RED if m['total_return_pct'] < 0 else C.GRAY
            print(f"  {symbol:<10} {short_name:<14} "
                  f"{ret_color}{ret_str:>10}{C.R} {m['n_trades']:>8} "
                  f"{m['win_rate_pct']:>8.1f}% {m['profit_factor']:>7.2f} "
                  f"{C.RED}-{m['max_drawdown_pct']:.1f}%{C.R}")
        print(f"  {C.GRAY}{'─' * 75}{C.R}")

    # Per-variant aggregate (consistency check)
    print(f"\n  {C.BOLD}Cross-symbol consistency per variant:{C.R}")
    for v_idx, v in enumerate(variants):
        rets = []
        trades_total = 0
        for symbol, sym_results in all_results.items():
            if v_idx < len(sym_results):
                rets.append(sym_results[v_idx][1]['total_return_pct'])
                trades_total += sym_results[v_idx][1]['n_trades']
        if rets:
            avg = statistics.mean(rets)
            consistent = all(r > 0 for r in rets) or all(r < 0 for r in rets)
            sign_text = (f"{C.GREEN}all positive{C.R}" if all(r > 0 for r in rets)
                        else f"{C.RED}all negative{C.R}" if all(r < 0 for r in rets)
                        else f"{C.YELLOW}mixed{C.R}")
            avg_color = C.GREEN if avg > 0 else C.RED if avg < 0 else C.GRAY
            print(f"    {v['name']:<16} avg {avg_color}{avg:+.2f}%{C.R} across {len(rets)} symbols, "
                  f"{trades_total} total trades, {sign_text}")

    print(f"\n{C.BOLD}{C.MAGENTA}╚══════════════════════════════════════════════════════════════════════════╝{C.R}\n")

    # Best detail trades
    best_combo = None
    best_ret = -float("inf")
    for symbol, sym_results in all_results.items():
        for r, m in sym_results:
            if m['total_return_pct'] > best_ret:
                best_ret = m['total_return_pct']
                best_combo = r
    if best_combo and best_combo["trades"]:
        print(f"  {C.GRAY}Best performer: {C.GREEN}{best_combo['name']}{C.R}")
        print_recent_trades(best_combo["trades"], n=15)

    # Save
    serializable = {}
    for symbol, sym_results in all_results.items():
        serializable[symbol] = []
        for r, m in sym_results:
            serializable[symbol].append({
                "name": r["name"], "metrics": m,
                "trades": [
                    {"ts": t.ts, "datetime": datetime.fromtimestamp(t.ts/1000, tz=timezone.utc).isoformat(),
                     "side": t.side, "price": t.price, "qty": t.qty,
                     "value_usd": t.value_usd, "fee_usd": t.fee_usd,
                     "pnl_usd": t.pnl_usd, "pnl_pct": t.pnl_pct, "reason": t.reason}
                    for t in r["trades"]
                ]
            })
    with open(args.output, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "interval": args.interval, "days": args.days,
                   "results": serializable}, f, indent=2, default=str)
    print(f"\n  {C.GREEN}✓{C.R} Results saved to {C.CYAN}{args.output}{C.R}\n")

    # Interpretation
    print(f"  {C.GRAY}What matters most:{C.R}")
    print(f"  • {C.BOLD}Cross-symbol consistency{C.R}: same variant positief op alle 3 = echte edge")
    print(f"  • {C.BOLD}PF > 1.3 + N > 100 trades{C.R} = statistisch significant")
    print(f"  • Eén symbol positief, twee negatief = ruis/luck, geen edge\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Interrupted.{C.R}")
    except Exception as e:
        print(f"\n{C.RED}Fatal: {e}{C.R}")
        raise
