"""
Paper Trader Summary
====================
Leest paper_state.json en print een leesbaar overzicht. Verandert NIETS
aan de state, alleen lezen. Bedoeld als snelle status-check tussendoor.

Gebruik:
  python paper_summary.py                     # leest lokale paper_state.json
  python paper_summary.py --from-github       # downloadt verse state uit jouw repo
  python paper_summary.py --state-file X.json # andere file
  python paper_summary.py --json              # output als JSON
  python paper_summary.py --no-color          # geen ANSI kleuren
  python paper_summary.py --telegram          # stuur naar Telegram (env vars vereist)

Wat je krijgt:
- Per variant: aantal trades, win rate, gem return, totaal P&L, beste/slechtste trade
- Open trades met tijd-tot-exit
- Cumulatief over alle variants
- Vergelijking met backtest-verwachting (hardcoded uit edge_test resultaten)
- Plain-language interpretatie: matcht live met backtest, of niet?
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta
from urllib import request as urlrequest, parse as urlparse


# Lokale tijdzone voor display (logging blijft UTC in JSON state).
# Op Linux (GitHub Actions) werkt ZoneInfo direct. Op Windows zonder
# tzdata package valt hij terug naar fixed CEST/CET offset.
def _resolve_local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Amsterdam")
    except Exception:
        # Fallback: detecteer DST via huidige datum (eind maart tot eind oktober = CEST)
        now = datetime.now(timezone.utc)
        if 3 < now.month < 10 or (now.month == 3 and now.day >= 25) or (now.month == 10 and now.day < 25):
            return timezone(timedelta(hours=2), name="CEST")
        return timezone(timedelta(hours=1), name="CET")


LOCAL_TZ = _resolve_local_tz()

# Default GitHub raw URL voor --from-github (pas aan als repo verhuist)
DEFAULT_REPO_RAW_URL = (
    "https://raw.githubusercontent.com/robbiebak007/pulselab-paper-trader/main/paper_state.json"
)


# Backtest-verwachtingen per variant (uit edge_test_*.json resultaten)
# Wordt gebruikt om live cijfers tegen te vergelijken.
BACKTEST_EXPECTATIONS: dict[str, dict] = {
    "stETH_15m_z3.5_180m_w200": {
        "label": "stETH/WPLS",
        "expected_mean_pct": 6.38,  # 300min hold, naam is legacy
        "expected_signals_per_day": 0.65,
        "config": "15m Z<-3.5 300min window 200 (extended, naam legacy)",
    },
    "PCOCK_15m_z3.5_180m_w100": {
        "label": "PCOCK/WPLS (gedropt)",
        "expected_mean_pct": 1.36,
        "expected_signals_per_day": 0.0,
        "config": "GEDROPT - 0/2 wins op extended hold, history bewaard",
    },
    "stETH_15m_z2.0_60m_w200_scalp": {
        "label": "stETH/WPLS scalp (gedropt)",
        "expected_mean_pct": 1.49,
        "expected_signals_per_day": 0.0,
        "config": "GEDROPT - +0.61% live = -0.09% na fees, history bewaard",
    },
    "INC_15m_z3.5_180m_w100": {
        "label": "INC/WPLS",
        "expected_mean_pct": 1.07,  # extended test 300min recent
        "expected_signals_per_day": 0.3,
        "config": "15m Z<-3.5 300min window 100 (extended, naam legacy)",
    },
    "WETH_5m_z3.0_90m_w100": {
        "label": "WETH/WPLS",
        "expected_mean_pct": 0.90,
        "expected_signals_per_day": 0.9,
        "config": "5m Z<-3.0 90min window 100",
    },
    "MOST_15m_z3.5_120m_w100": {
        "label": "MOST/WPLS (gedropt)",
        "expected_mean_pct": 0.74,
        "expected_signals_per_day": 0.0,
        "config": "GEDROPT - live tegengesteld aan backtest, history bewaard",
    },
    "HEX_15m_z3.5_120m_w100": {
        "label": "HEX/WPLS V1",
        "expected_mean_pct": 1.69,  # extended test 300min recent
        "expected_signals_per_day": 0.3,
        "config": "15m Z<-3.5 300min window 100 (extended, naam legacy)",
    },
    "DAI_WPLS_15m_z3.0_180m_w100": {
        "label": "DAI/WPLS (gedropt)",
        "expected_mean_pct": 1.31,
        "expected_signals_per_day": 0.0,
        "config": "GEDROPT - live underperformance, history bewaard",
    },
    "PLSX_15m_z3.5_120m_w100": {
        "label": "PLSX/WPLS",
        "expected_mean_pct": 1.50,  # extended test 300min recent
        "expected_signals_per_day": 0.3,
        "config": "15m Z<-3.5 300min window 100 (extended, naam legacy)",
    },
    "ATROPA_15m_z3.5_180m_w100": {
        "label": "ATROPA/DAI",
        "expected_mean_pct": 1.30,  # extended test 300min recent
        "expected_signals_per_day": 0.65,
        "config": "15m Z<-3.5 300min window 100 (extended, naam legacy)",
    },
    "CLUTCH_15m_z3.5_180m_w100_test": {
        "label": "CLUTCH/WPLS (test)",
        "expected_mean_pct": 0.0,  # bewust 0, geen edge verwacht
        "expected_signals_per_day": 0.3,
        "config": "15m Z<-3.5 180min window 100 (CONTROLE)",
    },
}


# ANSI kleuren
class C:
    R = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def _strip_colors() -> None:
    for attr in dir(C):
        if not attr.startswith("_"):
            setattr(C, attr, "")


if not (sys.stdout.isatty() and os.environ.get("TERM") != "dumb"):
    _strip_colors()


def color_pnl(value: float) -> str:
    if value > 0:
        return C.GREEN
    if value < 0:
        return C.RED
    return ""


def fmt_ts(ts: int) -> str:
    """Format unix timestamp in lokale tijd (Europe/Amsterdam)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ).strftime(
        "%Y-%m-%d %H:%M"
    )


def fmt_duration_h(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}min"
    return f"{seconds/3600:.1f}u"


def analyze_variant(name: str, data: dict) -> dict:
    closed = data.get("closed_trades", [])
    open_trade = data.get("open_trade")
    last_signal_ts = data.get("last_signal_ts", 0)

    n_trades = len(closed)
    wins = [t for t in closed if t["return_pct"] > 0]
    losses = [t for t in closed if t["return_pct"] < 0]
    breaks = [t for t in closed if t["return_pct"] == 0]

    returns = [t["return_pct"] for t in closed]
    pnls = [t["pnl_usd"] for t in closed]
    hold_minutes = [t["hold_minutes"] for t in closed]

    out: dict = {
        "name": name,
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_breakeven": len(breaks),
        "open_trade": open_trade,
        "last_signal_ts": last_signal_ts,
    }

    if n_trades > 0:
        out["mean_return_pct"] = statistics.fmean(returns)
        out["median_return_pct"] = statistics.median(returns)
        out["total_pnl_usd"] = sum(pnls)
        out["win_rate_pct"] = len(wins) / n_trades * 100
        out["best_trade_pct"] = max(returns)
        out["worst_trade_pct"] = min(returns)
        out["avg_hold_min"] = statistics.fmean(hold_minutes)
        out["first_trade_ts"] = min(t["entry_ts"] for t in closed)
        out["last_trade_ts"] = max(t["exit_ts"] for t in closed)
        out["days_active"] = (out["last_trade_ts"] - out["first_trade_ts"]) / 86400
        if len(returns) > 1:
            out["stdev_return_pct"] = statistics.stdev(returns)
            out["sem_pct"] = out["stdev_return_pct"] / math.sqrt(len(returns))
            # eenvoudige 95% CI op gemiddelde
            out["ci95_low"] = out["mean_return_pct"] - 1.96 * out["sem_pct"]
            out["ci95_high"] = out["mean_return_pct"] + 1.96 * out["sem_pct"]

    return out


def print_variant_block(stats: dict) -> None:
    name = stats["name"]
    exp = BACKTEST_EXPECTATIONS.get(name, {})
    label = exp.get("label", name)
    config = exp.get("config", "?")
    expected_mean = exp.get("expected_mean_pct")

    print(f"{C.BOLD}{label}{C.R} {C.DIM}({name}){C.R}")
    print(f"  {C.DIM}Config: {config}{C.R}")
    if expected_mean is not None:
        print(f"  {C.DIM}Backtest verwacht: +{expected_mean:.2f}% gem per trade{C.R}")

    n = stats["n_trades"]
    if n == 0:
        print(f"  {C.GRAY}Nog geen trades afgesloten.{C.R}")
        if stats["open_trade"]:
            ot = stats["open_trade"]
            sec_to_exit = ot["planned_exit_ts"] - int(datetime.now().timestamp())
            print(
                f"  {C.CYAN}1 open trade{C.R} sinds {fmt_ts(ot['entry_ts'])} "
                f"(Z={ot['z_score']:.2f}, exit over {fmt_duration_h(sec_to_exit)})"
            )
        if stats["last_signal_ts"]:
            print(
                f"  {C.DIM}Laatst gechecked tot candle {fmt_ts(stats['last_signal_ts'])}{C.R}"
            )
        print()
        return

    mean = stats["mean_return_pct"]
    total = stats["total_pnl_usd"]
    wr = stats["win_rate_pct"]
    best = stats["best_trade_pct"]
    worst = stats["worst_trade_pct"]
    avg_hold = stats["avg_hold_min"]

    col_mean = color_pnl(mean)
    col_total = color_pnl(total)
    sign_mean = "+" if mean >= 0 else ""
    sign_total = "+" if total >= 0 else ""
    sign_best = "+" if best >= 0 else ""
    sign_worst = "+" if worst >= 0 else ""

    print(
        f"  Trades: {n}  ({stats['n_wins']} win, {stats['n_losses']} loss, "
        f"{stats['n_breakeven']} break-even)"
    )
    print(f"  Win rate: {wr:.1f}%")
    print(
        f"  Mean return: {col_mean}{sign_mean}{mean:.3f}%{C.R}  "
        f"(median {stats['median_return_pct']:+.3f}%)"
    )
    print(
        f"  Totale P&L: {col_total}{sign_total}${total:.2f}{C.R} "
        f"({C.DIM}op $50 virtueel per trade{C.R})"
    )
    print(f"  Beste trade: {sign_best}{best:.2f}%   Slechtste: {sign_worst}{worst:.2f}%")
    print(f"  Gem. hold-tijd: {avg_hold:.0f} min")

    if "ci95_low" in stats:
        ci_low = stats["ci95_low"]
        ci_high = stats["ci95_high"]
        sig_marker = ""
        if ci_low > 0:
            sig_marker = f" {C.GREEN}[significant positief]{C.R}"
        elif ci_high < 0:
            sig_marker = f" {C.RED}[significant negatief]{C.R}"
        else:
            sig_marker = f" {C.GRAY}[ruis, te weinig data]{C.R}"
        print(f"  95% CI op mean: [{ci_low:+.3f}%, {ci_high:+.3f}%]{sig_marker}")

    # vergelijking met backtest
    if expected_mean is not None and n >= 5:
        ratio = mean / expected_mean if expected_mean != 0 else 0
        if ratio > 0.8:
            verdict = f"{C.GREEN}MATCH backtest verwachting{C.R}"
        elif ratio > 0.3:
            verdict = f"{C.YELLOW}Onder verwachting (~{ratio*100:.0f}% van backtest){C.R}"
        elif ratio > 0:
            verdict = f"{C.RED}Veel zwakker dan backtest{C.R}"
        else:
            verdict = f"{C.RED}Tegengesteld aan backtest{C.R}"
        print(f"  Vergelijking: {verdict}")
    elif n < 5:
        print(f"  {C.DIM}Vergelijking: te weinig trades voor conclusie (n<5){C.R}")

    if stats["open_trade"]:
        ot = stats["open_trade"]
        sec_to_exit = ot["planned_exit_ts"] - int(datetime.now().timestamp())
        print(
            f"  {C.CYAN}+ 1 open trade{C.R} sinds {fmt_ts(ot['entry_ts'])} "
            f"(Z={ot['z_score']:.2f}, exit over {fmt_duration_h(sec_to_exit)})"
        )

    if "first_trade_ts" in stats:
        print(
            f"  {C.DIM}Eerste trade: {fmt_ts(stats['first_trade_ts'])}   "
            f"Laatste exit: {fmt_ts(stats['last_trade_ts'])}{C.R}"
        )

    print()


def print_summary(state: dict) -> None:
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    saved_at = state.get("saved_at", "?")

    print(f"{C.BOLD}{'='*70}{C.R}")
    print(f"{C.BOLD}PAPER TRADER SUMMARY{C.R}   {C.DIM}(nu: {now}){C.R}")
    print(f"{C.DIM}State opgeslagen: {saved_at} (UTC){C.R}")
    print(f"{C.BOLD}{'='*70}{C.R}\n")

    variants_raw = state.get("variants", {})

    if not variants_raw:
        print(f"{C.YELLOW}Geen variants gevonden in state-file.{C.R}")
        print(f"{C.DIM}Is het script al eens gedraaid?{C.R}")
        return

    all_stats = []
    for name, data in variants_raw.items():
        stats = analyze_variant(name, data)
        all_stats.append(stats)
        print_variant_block(stats)

    # Cumulatief
    total_trades = sum(s["n_trades"] for s in all_stats)
    total_wins = sum(s["n_wins"] for s in all_stats)
    total_pnl = sum(s.get("total_pnl_usd", 0) for s in all_stats)
    open_count = sum(1 for s in all_stats if s["open_trade"])

    print(f"{C.BOLD}{'-'*70}{C.R}")
    print(f"{C.BOLD}TOTAAL OVER ALLE VARIANTS{C.R}")
    print(f"  Closed trades: {total_trades}  ({total_wins} winnaars)")
    if total_trades > 0:
        wr = total_wins / total_trades * 100
        col = color_pnl(total_pnl)
        sign = "+" if total_pnl >= 0 else ""
        print(f"  Win rate: {wr:.1f}%")
        print(f"  Totale P&L: {col}{sign}${total_pnl:.2f}{C.R}")
    print(f"  Open trades: {open_count}")
    print(f"{C.BOLD}{'-'*70}{C.R}\n")

    # Plain language interpretatie
    print(f"{C.BOLD}WAT DIT BETEKENT{C.R}")
    if total_trades == 0:
        print(
            f"  {C.GRAY}Nog te vroeg voor conclusies. Wacht tot eerste trades binnen zijn."
            f"{C.R}"
        )
    elif total_trades < 5:
        print(
            f"  {C.YELLOW}{total_trades} trade(s) is te weinig om iets te concluderen.{C.R}"
        )
        print(f"  {C.DIM}Wacht tot je minimaal 5-10 trades per variant hebt.{C.R}")
    elif total_trades < 20:
        print(
            f"  {C.YELLOW}{total_trades} trades geeft eerste indicatie, geen harde conclusie.{C.R}"
        )
        print(
            f"  {C.DIM}Voor betrouwbare backtest-vs-live vergelijking: 20+ trades per variant.{C.R}"
        )
    else:
        print(f"  {C.GREEN}Voldoende trades voor betekenisvolle vergelijking.{C.R}")
        for s in all_stats:
            if s["n_trades"] < 20:
                continue
            exp = BACKTEST_EXPECTATIONS.get(s["name"], {})
            expected = exp.get("expected_mean_pct", 0)
            live = s["mean_return_pct"]
            ci_low = s.get("ci95_low")
            label = exp.get("label", s["name"])
            print(
                f"  {label}: backtest +{expected:.2f}% gem, live {live:+.2f}% gem  "
                f"(CI [{ci_low:+.2f}%, {s.get('ci95_high', 0):+.2f}%])"
                if ci_low is not None
                else ""
            )
    print()


def build_telegram_message(state: dict) -> str:
    """
    Compacte HTML-geformatteerde digest voor Telegram. Geen ANSI, geen tabellen.
    """
    now_local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    now_str = now_local.strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"<b>Paper Trader Digest</b>", f"{now_str}", ""]

    variants_raw = state.get("variants", {})
    if not variants_raw:
        lines.append("Geen variants in state-file.")
        return "\n".join(lines)

    grand_trades = 0
    grand_wins = 0
    grand_pnl = 0.0
    grand_open = 0

    for name, data in variants_raw.items():
        stats = analyze_variant(name, data)
        exp = BACKTEST_EXPECTATIONS.get(name, {})
        label = exp.get("label", name)
        expected = exp.get("expected_mean_pct")

        lines.append(f"<b>{label}</b>")

        n = stats["n_trades"]
        if n == 0:
            if stats["open_trade"]:
                ot = stats["open_trade"]
                sec_left = ot["planned_exit_ts"] - int(datetime.now().timestamp())
                lines.append(
                    f"0 closed, 1 open (Z={ot['z_score']:.2f}, "
                    f"exit over {fmt_duration_h(sec_left)})"
                )
                grand_open += 1
            else:
                lines.append("Nog geen trades")
        else:
            wr = stats["win_rate_pct"]
            mean = stats["mean_return_pct"]
            total = stats["total_pnl_usd"]
            sign_mean = "+" if mean >= 0 else ""
            sign_total = "+" if total >= 0 else ""
            lines.append(
                f"{n} trades ({stats['n_wins']}W/{stats['n_losses']}L), WR {wr:.0f}%"
            )
            lines.append(
                f"Mean: {sign_mean}{mean:.2f}%  P&amp;L: {sign_total}${total:.2f}"
            )
            if expected is not None and n >= 5:
                ratio = mean / expected if expected != 0 else 0
                if ratio > 0.8:
                    verdict = "MATCH"
                elif ratio > 0.3:
                    verdict = f"~{ratio*100:.0f}% v backtest"
                elif ratio > 0:
                    verdict = "veel zwakker"
                else:
                    verdict = "tegengesteld"
                lines.append(f"vs backtest +{expected:.2f}%: {verdict}")
            if stats["open_trade"]:
                ot = stats["open_trade"]
                sec_left = ot["planned_exit_ts"] - int(datetime.now().timestamp())
                lines.append(
                    f"+ 1 open (Z={ot['z_score']:.2f}, exit {fmt_duration_h(sec_left)})"
                )
                grand_open += 1
            grand_trades += n
            grand_wins += stats["n_wins"]
            grand_pnl += total

        lines.append("")

    sign_pnl = "+" if grand_pnl >= 0 else ""
    lines.append(f"<b>Totaal</b>: {grand_trades} trades, {sign_pnl}${grand_pnl:.2f}")
    if grand_trades > 0:
        wr_total = grand_wins / grand_trades * 100
        lines.append(f"Win rate: {wr_total:.1f}%, open: {grand_open}")
    else:
        lines.append(f"Open: {grand_open}")

    return "\n".join(lines)


def send_telegram(text: str, token: str, chat_id: str) -> bool:
    """Stuur een bericht naar Telegram. Return True bij success."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlparse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    req = urlrequest.Request(url, data=data, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            return body.get("ok", False)
    except Exception as e:
        print(f"Telegram send faalde: {e}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Paper Trader summary report uit paper_state.json"
    )
    parser.add_argument(
        "--state-file", default="paper_state.json", help="Pad naar state-file"
    )
    parser.add_argument("--json", action="store_true", help="Output als JSON")
    parser.add_argument("--no-color", action="store_true", help="Geen ANSI kleuren")
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Stuur compacte digest naar Telegram (env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)",
    )
    parser.add_argument(
        "--from-github",
        nargs="?",
        const=DEFAULT_REPO_RAW_URL,
        metavar="URL",
        help=(
            "Download state direct van GitHub raw URL (default: jouw repo). "
            "Zo hoef je niet eerst handmatig paper_state.json te downloaden."
        ),
    )
    args = parser.parse_args()

    if args.no_color:
        _strip_colors()

    state: dict
    if args.from_github:
        url = args.from_github
        print(f"{C.DIM}Downloading state van {url}{C.R}")
        try:
            req = urlrequest.Request(
                url, headers={"User-Agent": "paper_summary", "Cache-Control": "no-cache"}
            )
            with urlrequest.urlopen(req, timeout=15) as resp:
                state = json.loads(resp.read())
        except Exception as e:
            print(f"{C.RED}Download faalde: {e}{C.R}")
            print(
                f"{C.DIM}Tip: is de repo public en bestaat paper_state.json al?{C.R}"
            )
            return 1
    else:
        if not os.path.exists(args.state_file):
            print(f"{C.RED}State-file niet gevonden: {args.state_file}{C.R}")
            print(
                f"{C.DIM}Tip: gebruik --from-github om direct van repo te downloaden,{C.R}"
            )
            print(
                f"{C.DIM}of draai dit script in dezelfde map als paper_state.json{C.R}"
            )
            return 1

        try:
            with open(args.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except json.JSONDecodeError as e:
            print(f"{C.RED}State-file niet leesbaar als JSON: {e}{C.R}")
            return 1

    if args.json:
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "state_saved_at": state.get("saved_at"),
            "variants": [
                analyze_variant(name, data)
                for name, data in state.get("variants", {}).items()
            ],
        }
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            print(
                f"{C.RED}--telegram vereist TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID env vars{C.R}"
            )
            return 1
        message = build_telegram_message(state)
        ok = send_telegram(message, token, chat_id)
        if ok:
            print(f"{C.GREEN}Digest naar Telegram verstuurd.{C.R}")
            return 0
        else:
            print(f"{C.RED}Telegram send faalde.{C.R}")
            return 1

    print_summary(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
