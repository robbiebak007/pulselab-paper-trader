"""
PulseChain Paper Trader
=======================
Live signal-detector en paper-trade-simulator op meerdere PulseChain pools
tegelijk. Polled GeckoTerminal, berekent rolling Z-score, opent virtuele
trades op signaal en sluit ze na de geplande horizon. Geen echte swaps,
geen wallet, geen risico.

Doel: vergelijk live data met backtest-verwachting (Fase 2a).

Variants worden bovenaan dit bestand geconfigureerd. Pas ze aan naar
smaak. Default zijn de PASS-combos uit edge_test_*.json:

  stETH/WPLS         15m  Z<-3.5  300min  window 200  (extended-best, +4.70% gross)
  INC/WPLS           15m  Z<-3.5  300min  window 100  (300 sweet spot, +1.07%)
  WETH/WPLS           5m  Z<-3.0   90min  window 100  (5m blijft 90min, +0.90%)
  HEX/WPLS V1        15m  Z<-3.5  300min  window 100  (300 optimum, +1.69%)
  PLSX/WPLS          15m  Z<-3.5  300min  window 100  (300 optimum, +1.50%)
  ATROPA/DAI         15m  Z<-3.5  300min  window 100  (live +2.55% bewezen)
  CLUTCH/WPLS (test) 15m  Z<-3.5  180min  window 100  (CONTROLE: geen edge verwacht)

Gedropt (history archiveerd in state): PCOCK, stETH scalp, MOST, DAI/WPLS.
Reden: live data wees ze uit als onder backtest of zelfs negatief.

Gebruik:
  python paper_trader.py                       # terminal-only, 60s polling
  python paper_trader.py --interval 30         # poll elke 30s
  python paper_trader.py --once                # één cyclus dan exit (test)
  python paper_trader.py --telegram-token X --telegram-chat Y

Telegram opzetten (optioneel):
  1. In Telegram: zoek @BotFather, /newbot, kies naam, krijg TOKEN
  2. Start chat met je bot, stuur /start
  3. In Telegram: zoek @userinfobot, /start, krijg jouw CHAT_ID
  4. Geef ze mee via --telegram-token en --telegram-chat
     (of zet als env vars TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID)

Requirements: alleen stdlib.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlrequest, parse as urlparse, error as urlerror


# ============================================================
# CONFIGURATIE - hier passen
# ============================================================

VARIANTS: list[dict[str, Any]] = [
    {
        # V4b + extended hold: window=200, horizon van 180->300min.
        # Extended-horizon test: recent +6.38% en OOS +13.87% op 300min,
        # beide periodes PEAK hier. Cross-period validated upgrade.
        # NAAM houdt "180m" voor history-continuiteit, ECHTE config is 300m.
        "name": "stETH_15m_z3.5_180m_w200",
        "label": "stETH/WPLS",
        "pool": "0x34243b6878cb49530B2B647F38AA26623dab2509",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 300,
        "window": 200,
    },
    {
        # INC/WPLS: cross-period gevalideerd. Extended-extended test toonde
        # 300min als optimum (+1.07% vs +0.64% op 180min). Backtest-recent
        # bewezen, geen OOS validatie op 300 maar 300 was sweet spot bij 5/6
        # andere pools dus pattern-bevestiging.
        "name": "INC_15m_z3.5_180m_w100",
        "label": "INC/WPLS",
        "pool": "0xf808Bb6265e9Ca27002c0A04562Bf50d4FE37EAA",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 300,
        "window": 100,
    },
    {
        # WETH/WPLS: 5m timeframe blijft, 90min hold blijft.
        # Target-exit backtest bewijst +2% take-profit target verhoogt P&L
        # met +30.9% (van $29.51 naar $38.63 over 78 trades). 5m kort
        # horizon = korte bounces lokaal piek, target lockt gains vast.
        "name": "WETH_5m_z3.0_90m_w100",
        "label": "WETH/WPLS",
        "pool": "0x42AbdFDB63f3282033C766E72Cc4810738571609",
        "timeframe": "5m",
        "threshold": -3.0,
        "horizon_min": 90,
        "window": 100,
        "target_pct": 2.0,  # take-profit op +2% return
    },
    {
        # HEX/WPLS V1: extended test bevestigd 300min als optimum
        # (+1.69% vs +1.30% op 180min). NAAM houdt "120m" voor history.
        "name": "HEX_15m_z3.5_120m_w100",
        "label": "HEX/WPLS V1",
        "pool": "0xf1F4ee610b2bAbB05C635F726eF8B0C568c8dc65",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 300,
        "window": 100,
    },
    {
        # PLSX/WPLS: extended test toonde 300min duidelijk optimum (+1.50%
        # vs +1.13% op 180min). Pattern-bevestiging op 5/6 pools voor 300.
        "name": "PLSX_15m_z3.5_120m_w100",
        "label": "PLSX/WPLS",
        "pool": "0x1b45b9148791d3a104184cd5dfe5ce57193a3ee9",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 300,
        "window": 100,
    },
    {
        # ATROPA/DAI: extended test toonde monotone verbetering. 300min
        # +1.30%, 480min +1.50%. Gekozen voor 300 voor consistentie met
        # andere variants. Live data wijst al uit dat dit een sterke pool is.
        "name": "ATROPA_15m_z3.5_180m_w100",
        "label": "ATROPA/DAI",
        "pool": "0x5ef7aac0de4f2012cb36730da140025b113fada4",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 300,
        "window": 100,
    },
    {
        # CLUTCH/WPLS: CONTROLE-VARIANT, expliciet GEEN edge in backtest.
        # Liquiditeit slechts $3K = artifact-gevoelig. Bewust toegevoegd
        # om te toetsen of onze methodologie "geen edge" goed voorspelt.
        # Verwachting: 0 winstgevende trades over de meetperiode.
        "name": "CLUTCH_15m_z3.5_180m_w100_test",
        "label": "CLUTCH/WPLS (test)",
        "pool": "0x76B2e69e133945fE6a5ac8Cb6473c8257B26D401",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 180,
        "window": 100,
    },
    {
        # SCADA/WPLS: TEST-VARIANT toegevoegd door gebruiker.
        # Liquiditeit $35K, volume $2.4K/dag = klein pool, artifact-risico.
        # Geen backtest bewijs, verwacht vergelijkbaar met CLUTCH profiel.
        "name": "SCADA_15m_z3.5_180m_w100_test",
        "label": "SCADA/WPLS (test)",
        "pool": "0x629075c537633132C645a18F265d59e4153CE1C6",
        "timeframe": "15m",
        "threshold": -3.5,
        "horizon_min": 180,
        "window": 100,
    },
]

POSITION_SIZE_USD = 50.0  # virtueel kapitaal per trade
DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_STATE_FILE = "paper_state.json"


# ============================================================
# Constants / API
# ============================================================

BASE_URL = "https://api.geckoterminal.com/api/v2"
NETWORK = "pulsechain"
HEADERS = {
    "Accept": "application/json;version=20230302",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
RATE_LIMIT_DELAY = 2.5  # seconden tussen API calls binnen één cyclus

TIMEFRAME_MAP: dict[str, tuple[str, int, int]] = {
    # label -> (gecko_timeframe, gecko_aggregate, minutes_per_candle)
    "5m": ("minute", 5, 5),
    "15m": ("minute", 15, 15),
    "30m": ("minute", 30, 30),
    "1h": ("hour", 1, 60),
}


# ============================================================
# ANSI kleuren
# ============================================================

class C:
    R = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def _strip_colors() -> None:
    for attr in dir(C):
        if not attr.startswith("_"):
            setattr(C, attr, "")


if not (sys.stdout.isatty() and os.environ.get("TERM") != "dumb"):
    _strip_colors()


# ============================================================
# Data classes
# ============================================================

@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class OpenTrade:
    entry_ts: int
    entry_price: float
    planned_exit_ts: int
    z_score: float
    horizon_min: int
    target_pct: float = 0.0  # 0 = geen target, alleen time-exit


@dataclass
class ClosedTrade:
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    return_pct: float
    pnl_usd: float
    hold_minutes: int
    z_score: float


@dataclass
class VariantState:
    last_signal_ts: int = 0
    open_trade: OpenTrade | None = None
    closed_trades: list[ClosedTrade] = field(default_factory=list)


# ============================================================
# HTTP helpers
# ============================================================

def http_get(url: str, retries: int = 4) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urlrequest.Request(url, headers=HEADERS)
            with urlrequest.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urlerror.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = (attempt + 1) * 10
                _log(f"{C.YELLOW}rate limited (429), wacht {wait}s{C.R}")
                time.sleep(wait)
            elif e.code == 403:
                raise RuntimeError(
                    "HTTP 403 Forbidden van GeckoTerminal. Wacht 5 min en probeer opnieuw."
                )
            else:
                wait = (attempt + 1) * 3
                _log(f"{C.YELLOW}HTTP {e.code}, retry {attempt+1}/{retries} in {wait}s{C.R}")
                time.sleep(wait)
        except (urlerror.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            wait = (attempt + 1) * 3
            _log(f"{C.YELLOW}netwerkfout, retry {attempt+1}/{retries} in {wait}s ({e}){C.R}")
            time.sleep(wait)
    raise RuntimeError(f"HTTP faalde na {retries} pogingen: {last_err}")


def fetch_pool_info(address: str) -> dict:
    url = f"{BASE_URL}/networks/{NETWORK}/pools/{address}"
    return http_get(url)["data"]


def fetch_recent_candles(address: str, timeframe_label: str, limit: int = 500) -> list[Candle]:
    """
    Pak de laatste `limit` candles. Default 500 = ruim genoeg voor window=200
    (V4b config) plus voldoende historie om de actie-candle correct te detecteren
    en exit-candles van oudere open trades terug te vinden.
    """
    tf, agg, _ = TIMEFRAME_MAP[timeframe_label]
    url = (
        f"{BASE_URL}/networks/{NETWORK}/pools/{address}/ohlcv/{tf}"
        f"?aggregate={agg}&limit={limit}"
    )
    data = http_get(url)
    rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    candles = [
        Candle(
            ts=int(r[0]),
            o=float(r[1]),
            h=float(r[2]),
            l=float(r[3]),
            c=float(r[4]),
            v=float(r[5]),
        )
        for r in rows
        if r and r[4] is not None
    ]
    candles.sort(key=lambda x: x.ts)
    return candles


# ============================================================
# Telegram client
# ============================================================

class Telegram:
    def __init__(self, token: str | None, chat_id: str | None):
        self.enabled = bool(token and chat_id)
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urlparse.urlencode(
            {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = urlrequest.Request(url, data=data, method="POST")
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            _log(f"{C.YELLOW}Telegram send faalde: {e}{C.R}")


# ============================================================
# Z-score en signaal-detectie
# ============================================================

def log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            out.append(0.0)
        else:
            out.append(math.log(closes[i] / closes[i - 1]))
    return out


def latest_zscore(closes: list[float], window: int) -> float | None:
    """Z-score van de meest recente return."""
    rets = log_returns(closes)
    if len(rets) < window + 1:
        return None
    last_window = rets[-(window + 1) : -1]  # exclusief de laatste, voor mean/std
    last_ret = rets[-1]
    mean = statistics.fmean(last_window + [last_ret])
    std = statistics.pstdev(last_window + [last_ret])
    if std == 0:
        return None
    return (last_ret - mean) / std


# ============================================================
# State persistentie
# ============================================================

# Globaal cache voor gedropte variants (zodat hun history behouden blijft)
_ARCHIVED_VARIANTS: dict[str, dict] = {}


def load_state(path: str) -> dict[str, VariantState]:
    global _ARCHIVED_VARIANTS
    if not os.path.exists(path):
        _ARCHIVED_VARIANTS = {}
        return {v["name"]: VariantState() for v in VARIANTS}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log(f"{C.YELLOW}State-file corrupt of onleesbaar: {e}. Verse start.{C.R}")
        _ARCHIVED_VARIANTS = {}
        return {v["name"]: VariantState() for v in VARIANTS}

    active_names = {v["name"] for v in VARIANTS}
    out: dict[str, VariantState] = {}
    for v in VARIANTS:
        d = raw.get("variants", {}).get(v["name"], {})
        open_trade = None
        if d.get("open_trade"):
            open_trade = OpenTrade(**d["open_trade"])
        closed = [ClosedTrade(**c) for c in d.get("closed_trades", [])]
        out[v["name"]] = VariantState(
            last_signal_ts=d.get("last_signal_ts", 0),
            open_trade=open_trade,
            closed_trades=closed,
        )

    # Archief: bewaar history van variants die niet meer in VARIANTS staan
    _ARCHIVED_VARIANTS = {
        name: data for name, data in raw.get("variants", {}).items()
        if name not in active_names
    }
    return out


def save_state(path: str, state: dict[str, VariantState]) -> None:
    variants_payload = {
        name: {
            "last_signal_ts": s.last_signal_ts,
            "open_trade": asdict(s.open_trade) if s.open_trade else None,
            "closed_trades": [asdict(c) for c in s.closed_trades],
        }
        for name, s in state.items()
    }
    # Voeg gearchiveerde variants weer toe (behoud history)
    variants_payload.update(_ARCHIVED_VARIANTS)
    raw = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "variants": variants_payload,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    os.replace(tmp, path)


# ============================================================
# Trade lifecycle
# ============================================================

def closed_candle_index(candles: list[Candle], timeframe_label: str) -> int | None:
    """
    Index van de meest recente VOLLEDIG GESLOTEN candle.
    Een candle [ts, ..., ts+interval) is gesloten als ts+interval <= now.
    """
    if not candles:
        return None
    _, _, minutes_per = TIMEFRAME_MAP[timeframe_label]
    interval_s = minutes_per * 60
    now = int(time.time())
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].ts + interval_s <= now:
            return i
    return None


def find_exit_candle(candles: list[Candle], target_ts: int) -> Candle | None:
    """Eerste candle waarvan ts >= target_ts."""
    for c in candles:
        if c.ts >= target_ts:
            return c
    return None


def maybe_open_trade(
    variant: dict[str, Any],
    state: VariantState,
    candles: list[Candle],
) -> OpenTrade | None:
    """
    Itereert over ALLE candles tussen last_signal_ts en de huidige latest
    closed candle. Vuurt op de EERSTE candle met Z < threshold. Voorkomt
    dat snelle achtereenvolgende dump-events worden overgeslagen wanneer
    de cron op een non-signaal candle landde tussen twee dump candles in.
    """
    if state.open_trade is not None:
        return None  # al een trade open, geen pyramiding in v1

    sig_idx_max = closed_candle_index(candles, variant["timeframe"])
    if sig_idx_max is None or sig_idx_max < variant["window"]:
        return None

    # Itereer van oudste niet-gecheckte candle naar nieuwste
    for i in range(variant["window"], sig_idx_max + 1):
        candle = candles[i]
        if candle.ts <= state.last_signal_ts:
            continue  # al gezien in vorige cyclus

        # Bereken Z-score met data t/m candle i (geen lookahead)
        closes_so_far = [c.c for c in candles[: i + 1]]
        z = latest_zscore(closes_so_far, variant["window"])

        if z is not None and z < variant["threshold"]:
            # Signaal vuurt op deze candle
            entry_ts = candle.ts
            entry_price = candle.c
            horizon_min = variant["horizon_min"]
            planned_exit_ts = entry_ts + horizon_min * 60
            target_pct = float(variant.get("target_pct", 0.0))

            trade = OpenTrade(
                entry_ts=entry_ts,
                entry_price=entry_price,
                planned_exit_ts=planned_exit_ts,
                z_score=z,
                horizon_min=horizon_min,
                target_pct=target_pct,
            )
            state.open_trade = trade
            state.last_signal_ts = candle.ts
            return trade

    # Geen signaal gevonden in alle niet-gecheckte candles
    state.last_signal_ts = candles[sig_idx_max].ts
    return None


def maybe_close_trade(
    variant: dict[str, Any],
    state: VariantState,
    candles: list[Candle],
) -> ClosedTrade | None:
    """
    Exit-logica: check EERST of target-price geraakt is (bij configured target_pct),
    dan pas time-based exit op planned_exit_ts. Target-check kijkt naar HIGH van
    tussenliggende candles (limit-order simulatie).
    """
    if state.open_trade is None:
        return None

    sig_idx = closed_candle_index(candles, variant["timeframe"])
    if sig_idx is None:
        return None

    entry_ts = state.open_trade.entry_ts
    entry_price = state.open_trade.entry_price
    if entry_price <= 0:
        return None

    # Target-check: als target_pct > 0, kijk of HIGH van enige candle na entry
    # target_price heeft geraakt VOOR de time-exit
    target_pct = state.open_trade.target_pct
    if target_pct > 0:
        target_price = entry_price * (1 + target_pct / 100)
        for c in candles:
            if c.ts <= entry_ts:
                continue
            if c.ts >= state.open_trade.planned_exit_ts:
                break  # planned exit voorbij, target-check klaar
            if c.h >= target_price:
                # Target geraakt: exit op target-prijs op deze candle
                return_pct = target_pct  # exact target return
                pnl_usd = POSITION_SIZE_USD * return_pct / 100
                hold_minutes = round((c.ts - entry_ts) / 60)
                closed = ClosedTrade(
                    entry_ts=entry_ts,
                    entry_price=entry_price,
                    exit_ts=c.ts,
                    exit_price=target_price,
                    return_pct=return_pct,
                    pnl_usd=pnl_usd,
                    hold_minutes=hold_minutes,
                    z_score=state.open_trade.z_score,
                )
                state.closed_trades.append(closed)
                state.open_trade = None
                return closed

    # Geen target hit of geen target geconfigureerd: time-based exit
    last_closed = candles[sig_idx]
    if last_closed.ts < state.open_trade.planned_exit_ts:
        return None  # nog niet aan tijd

    exit_candle = find_exit_candle(candles, state.open_trade.planned_exit_ts)
    if exit_candle is None:
        return None

    exit_price = exit_candle.c
    return_pct = (exit_price - entry_price) / entry_price * 100
    pnl_usd = POSITION_SIZE_USD * return_pct / 100
    hold_minutes = round((exit_candle.ts - entry_ts) / 60)

    closed = ClosedTrade(
        entry_ts=entry_ts,
        entry_price=entry_price,
        exit_ts=exit_candle.ts,
        exit_price=exit_price,
        return_pct=return_pct,
        pnl_usd=pnl_usd,
        hold_minutes=hold_minutes,
        z_score=state.open_trade.z_score,
    )
    state.closed_trades.append(closed)
    state.open_trade = None
    return closed


# ============================================================
# Loggen + dashboard
# ============================================================

def _ts_str(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{C.GRAY}[{stamp}]{C.R} {msg}")


def print_dashboard(state: dict[str, VariantState]) -> None:
    print()
    print(f"{C.BOLD}{'='*88}{C.R}")
    print(f"{C.BOLD}PAPER TRADER DASHBOARD - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{C.R}")
    print(f"{C.BOLD}{'='*88}{C.R}")
    print(
        f"{C.BOLD}{'Variant':<25}{'Trades':>8}{'Wins':>8}{'WinRate':>10}"
        f"{'AvgRet':>10}{'TotalP&L':>12}  Open?{C.R}"
    )
    print(C.GRAY + "-" * 88 + C.R)

    grand_total_pnl = 0.0
    grand_total_trades = 0

    for v in VARIANTS:
        s = state[v["name"]]
        n = len(s.closed_trades)
        wins = sum(1 for c in s.closed_trades if c.return_pct > 0)
        win_rate = (wins / n * 100) if n else 0.0
        avg_ret = (sum(c.return_pct for c in s.closed_trades) / n) if n else 0.0
        total_pnl = sum(c.pnl_usd for c in s.closed_trades)
        grand_total_pnl += total_pnl
        grand_total_trades += n

        open_mark = ""
        if s.open_trade:
            mins_left = max(0, round((s.open_trade.planned_exit_ts - time.time()) / 60))
            open_mark = f"{C.CYAN}OPEN ({mins_left}min){C.R}"

        col_pnl = C.GREEN if total_pnl > 0 else (C.RED if total_pnl < 0 else "")
        col_ret = C.GREEN if avg_ret > 0 else (C.RED if avg_ret < 0 else "")

        print(
            f"{v['name']:<25}{n:>8}{wins:>8}{win_rate:>9.1f}%"
            f"{col_ret}{avg_ret:>9.2f}%{C.R}"
            f"{col_pnl}{total_pnl:>11.2f}{C.R}  {open_mark}"
        )

    print(C.GRAY + "-" * 88 + C.R)
    col = C.GREEN if grand_total_pnl > 0 else (C.RED if grand_total_pnl < 0 else "")
    print(
        f"{C.BOLD}{'TOTAAL':<25}{grand_total_trades:>8}{'':>8}{'':>10}{'':>10}"
        f"{col}{grand_total_pnl:>11.2f}{C.R}{C.BOLD}{C.R}"
    )
    print(f"{C.DIM}Positie-grootte per trade: ${POSITION_SIZE_USD:.0f} virtueel{C.R}")
    print()


# ============================================================
# Cyclus
# ============================================================

def one_cycle(
    state: dict[str, VariantState],
    telegram: Telegram,
    state_file: str,
) -> None:
    candles_cache: dict[tuple[str, str], list[Candle]] = {}

    for v in VARIANTS:
        key = (v["pool"], v["timeframe"])
        if key not in candles_cache:
            try:
                candles_cache[key] = fetch_recent_candles(v["pool"], v["timeframe"], limit=300)
            except Exception as e:
                _log(f"{C.RED}Fetch faal {v['label']} {v['timeframe']}: {e}{C.R}")
                continue
            time.sleep(RATE_LIMIT_DELAY)

        candles = candles_cache[key]
        if not candles:
            _log(f"{C.YELLOW}Geen candles voor {v['label']} {v['timeframe']}{C.R}")
            continue

        s = state[v["name"]]

        # Eerst checken of open trade exit-rijp is
        closed = maybe_close_trade(v, s, candles)
        if closed:
            color = C.GREEN if closed.return_pct > 0 else C.RED
            sign = "+" if closed.return_pct >= 0 else ""
            msg_term = (
                f"{C.BOLD}EXIT{C.R} {v['name']} "
                f"entry ${closed.entry_price:.6g} -> exit ${closed.exit_price:.6g} "
                f"= {color}{sign}{closed.return_pct:.2f}% ({sign}${closed.pnl_usd:.2f}){C.R} "
                f"hold {closed.hold_minutes}min"
            )
            _log(msg_term)
            telegram.send(
                f"<b>EXIT {v['label']}</b>\n"
                f"Variant: {v['name']}\n"
                f"Entry: {closed.entry_price:.6g}\n"
                f"Exit: {closed.exit_price:.6g}\n"
                f"Return: {sign}{closed.return_pct:.2f}%\n"
                f"P&amp;L: {sign}${closed.pnl_usd:.2f}\n"
                f"Hold: {closed.hold_minutes} min"
            )

        # Daarna checken of een nieuw signaal vuurt
        opened = maybe_open_trade(v, s, candles)
        if opened:
            msg_term = (
                f"{C.BOLD}{C.CYAN}OPEN{C.R} {v['name']} "
                f"z={opened.z_score:.2f} entry ${opened.entry_price:.6g} "
                f"exit gepland {_ts_str(opened.planned_exit_ts)}"
            )
            _log(msg_term)
            telegram.send(
                f"<b>OPEN {v['label']}</b>\n"
                f"Variant: {v['name']}\n"
                f"Z-score: {opened.z_score:.2f} (drempel {v['threshold']})\n"
                f"Entry: {opened.entry_price:.6g}\n"
                f"Horizon: {opened.horizon_min} min\n"
                f"Geplande exit: {_ts_str(opened.planned_exit_ts)}"
            )

    save_state(state_file, state)


# ============================================================
# Main
# ============================================================

def validate_pools() -> bool:
    """Check eenmalig dat alle pools bestaan en print naam + liquiditeit."""
    print(f"{C.CYAN}Pools valideren...{C.R}")
    ok = True
    seen: dict[str, dict] = {}
    for v in VARIANTS:
        if v["pool"] in seen:
            info = seen[v["pool"]]
        else:
            try:
                info = fetch_pool_info(v["pool"])
                seen[v["pool"]] = info
                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                print(f"  {C.RED}{v['label']}: {e}{C.R}")
                ok = False
                continue
        attrs = info.get("attributes", {})
        name = attrs.get("name", "?")
        liq = float(attrs.get("reserve_in_usd") or 0)
        vol = float(attrs.get("volume_usd", {}).get("h24") or 0)
        print(
            f"  {C.GREEN}OK{C.R} {v['name']:<25} {name:<25} "
            f"liq=${liq:>11,.0f}  vol24h=${vol:>9,.0f}"
        )
    print()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PulseChain Paper Trader (Fase 2a) - signalen detecteren en virtuele trades tracken."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL_S,
        help=f"Poll-interval in seconden (default {DEFAULT_POLL_INTERVAL_S})",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"State-file pad (default {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--telegram-token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN"),
        help="Telegram bot token (of env TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--telegram-chat",
        default=os.environ.get("TELEGRAM_CHAT_ID"),
        help="Telegram chat ID (of env TELEGRAM_CHAT_ID)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Draai één cyclus dan exit (test-mode)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="ANSI kleuren uit",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Sla pool-validatie aan start over",
    )
    args = parser.parse_args()

    if args.no_color:
        _strip_colors()

    telegram = Telegram(args.telegram_token, args.telegram_chat)

    print(f"{C.BOLD}PulseChain Paper Trader{C.R}")
    print(f"  Variants: {len(VARIANTS)}")
    print(f"  Poll-interval: {args.interval}s")
    print(f"  State-file: {args.state_file}")
    print(f"  Telegram: {'AAN' if telegram.enabled else 'UIT'}")
    print(f"  Positie per trade: ${POSITION_SIZE_USD:.0f} virtueel")
    print()

    if not args.skip_validate:
        if not validate_pools():
            print(f"{C.RED}Pool-validatie faalde. Fix de pool-adressen of gebruik --skip-validate.{C.R}")
            return 1

    state = load_state(args.state_file)

    if args.once:
        one_cycle(state, telegram, args.state_file)
        print_dashboard(state)
        return 0

    if telegram.enabled:
        telegram.send(
            f"<b>Paper Trader gestart</b>\n"
            f"Variants: {len(VARIANTS)}\n"
            f"Poll: elke {args.interval}s"
        )

    print(f"{C.GREEN}Loop gestart. Ctrl+C om te stoppen.{C.R}\n")
    last_dashboard_min = -1
    try:
        while True:
            one_cycle(state, telegram, args.state_file)
            now_min = datetime.now().minute
            # Dashboard elke 5 min
            if now_min % 5 == 0 and now_min != last_dashboard_min:
                print_dashboard(state)
                last_dashboard_min = now_min
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Gestopt door gebruiker.{C.R}")
        print_dashboard(state)
        if telegram.enabled:
            telegram.send("Paper Trader gestopt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
