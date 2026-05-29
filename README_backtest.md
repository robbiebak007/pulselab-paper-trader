# PulseLab Backtest — Quick Start

Een echte backtest engine voor PulseChain trading strategieën. Gebruikt
historische OHLCV data van GeckoTerminal (gratis API, geen account nodig).

## Wat je krijgt

Voor elke pool wordt elke strategie getest met realistische executie:

- **PulseX 0.29% fee** per swap (één kant)
- **Slippage** geschat o.b.v. pool liquiditeit (constant-product AMM formule)
- **Geen lookahead bias**

Per strategie per pool zie je:
- Total return %
- Aantal trades + win rate
- Profit factor (gross win / gross loss)
- Max drawdown
- Sharpe ratio (geannualiseerd)
- Totaal aan fees betaald

Alles wordt opgeslagen in `pulselab_results.json` voor verdere analyse.

## Installatie (3 stappen)

### 1. Python installeren

Heb je Python 3.10+? Check met:

```bash
python3 --version
```

Zo niet, download van [python.org](https://www.python.org/downloads/) of via
package manager (`brew install python3` op Mac, `apt install python3` op
Linux). Op Windows: kies bij installatie "Add Python to PATH".

### 2. Het script downloaden

Sla `pulselab_backtest.py` op in een lege map. Het script gebruikt alleen
de Python standaardbibliotheek — geen `pip install` nodig.

### 3. Draaien

```bash
# Default: top 5 pools, 1h candles, 500 candles (~3 weken data)
python3 pulselab_backtest.py

# Top 10 pools, dagcandles voor langere historie
python3 pulselab_backtest.py --pools 10 --timeframe 1d --limit 365

# Specifieke pool (kopieer adres van GeckoTerminal of DexScreener URL)
python3 pulselab_backtest.py --pool 0xe56043671df55de5cdf8459710433c10324de0ae --timeframe 1h

# Strenger filter
python3 pulselab_backtest.py --pools 10 --min-liquidity 100000
```

## Alle opties

```
--pools N            Aantal top pools om te testen (default 5)
--pool ADDRESS       Specifiek pool address (overruled --pools)
--timeframe TF       1m / 5m / 15m / 1h / 4h / 1d  (default 1h)
--limit N            Max candles per pool, max 1000  (default 500)
--min-liquidity X    Skip pools met liquiditeit onder X USD (default 50000)
--output FILE        JSON output bestand (default pulselab_results.json)
```

## Hoe lang duurt het?

GeckoTerminal free tier limiteert op ~30 calls/minuut. Het script wacht
2.5s tussen calls. Reken op:
- 5 pools  → ~30 seconden
- 10 pools → ~1 minuut
- 50 pools → ~5 minuten

## Hoe je de resultaten leest

```
● Mean Reversion
    return:              +12.40%        final eq:    $11,240.00
    trades:                       8     win rate:        62.5%
    wins/losses:    5W·3L                profit factor:     2.30
    avg win:              +4.20%        avg loss:       -2.10%
    max drawdown:        -3.80%         sharpe (ann):     1.45
```

**Wat is goed:**
- Win rate alleen zegt niks (kleine winsten + grote verliezen = bankroet)
- **Profit factor > 1.5** met **30+ trades** = mogelijk echte edge
- **Sharpe > 1.0** = redelijke risico-gecorrigeerde return
- **Max drawdown** = kun je dit emotioneel verdragen live?

**Wat verdacht is:**
- 100% win rate met 3 trades = niet significant
- Hoge return met max drawdown >50% = roulette
- Eén strategie wint op één pool, verliest op alle andere = lucky

## Realistische verwachting

De meeste strategieën zullen geld verliezen na fees en slippage. Dat is
**niet** een bug — dat is de markt. Als ze hier al verliezen, doen ze het
live nog slechter (MEV, gas-spikes, vertraging).

Doelen voor "echte" strategie:
- Consistent positief over **meerdere pools** (5+)
- Positief over **meerdere tijdvakken** (verschillende candle sizes)
- **30+ trades** per pool minimaal
- Profit factor > 1.5 na fees

Als één strategie deze drie boxes aankruist op jouw filter-set, heb je
iets dat *misschien* werkt. Test dan eerst weken paper trading op live
data, dan pas live met klein bedrag.

## Output JSON gebruiken

Het `pulselab_results.json` bestand bevat alle trades met timestamps, P&L,
en reden. Open in Excel, Python pandas, of bouw eigen visualisaties:

```python
import json
data = json.load(open('pulselab_results.json'))
for pool in data['results']:
    for strat in pool['strategies']:
        print(pool['pool']['name'], strat['name'],
              strat['metrics']['total_return_pct'])
```

## Disclaimers

- Backtests overschatten altijd toekomstige returns
- "Werkt in backtest" ≠ "werkt live" (slippage, MEV, gas, latency)
- Survivorship bias: gerugde tokens staan niet in GeckoTerminal
- Geen advies — alleen een onderzoekstool

Bouw eerst conviction, dan klein geld, dan misschien meer.
