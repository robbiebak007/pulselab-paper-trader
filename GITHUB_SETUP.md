# GitHub Actions setup voor Paper Trader

## Wat dit oplevert

Paper trader draait elke 5 minuten op een GitHub server, 24/7, gratis. Voor altijd. State persistente in de repo zodat herstart niets verliest. Telegram alerts optioneel.

## Stappenplan

### 1. GitHub account

Als je er nog geen hebt: ga naar github.com/signup en maak een account. Verifieer je email.

### 2. Nieuwe repo aanmaken

1. Klik rechtsbovenin op het plusje, kies "New repository"
2. Naam: `pulselab-paper-trader` (of wat je wil)
3. Visibility: **Public** (dan zijn GitHub Actions volledig gratis voor 5-min cron)
4. Vink aan: "Add a README file"
5. Klik "Create repository"

> Public repo: ja, iedereen kan de code zien. Maar er staan geen wallet keys of API credentials in de code. Telegram bot token komt in encrypted secrets, niet zichtbaar. Volledig veilig.

### 3. Bestanden uploaden

Vanuit deze map (`C:\Users\rober\Desktop\Pulselab`) moeten deze bestanden in de repo:

- `paper_trader.py`
- `.github/workflows/paper-trader.yml`
- `.gitignore`
- `paper_state.json` (initiële lege state, wordt automatisch aangemaakt na eerste run)

**Eenvoudigste manier (via browser)**:

1. Op je nieuwe repo-pagina klik "Add file" -> "Upload files"
2. Sleep `paper_trader.py` en `.gitignore` erin
3. Scroll naar beneden, klik "Commit changes"
4. Voor de workflow YAML: klik "Add file" -> "Create new file"
5. Naam: `.github/workflows/paper-trader.yml` (typ de hele path, GitHub maakt automatisch de folders)
6. Plak de inhoud van `paper-trader.yml` erin
7. Klik "Commit changes"

**Met git CLI (sneller als je git geinstalleerd hebt)**:

```powershell
cd C:\Users\rober\Desktop\Pulselab
git init
git add paper_trader.py .gitignore .github/workflows/paper-trader.yml
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/JOUW_GEBRUIKERSNAAM/pulselab-paper-trader.git
git push -u origin main
```

### 4. Workflow activeren

GitHub activeert workflows niet automatisch bij eerste upload op gratis accounts. Eenmalig handmatig starten:

1. Ga naar tabblad "Actions"
2. Klik bij "Paper Trader" workflow op "I understand my workflows, go ahead and enable them" (als die er staat)
3. Klik rechts op "Run workflow" -> "Run workflow" om de eerste cyclus handmatig te starten
4. Wacht ongeveer 1-2 minuten

Daarna start hij automatisch elke 5 minuten via de cron.

### 5. Telegram aanzetten (optioneel)

Als je Telegram alerts wil:

1. In Telegram: zoek `@BotFather`, stuur `/newbot`, kies naam en username, krijg een token
2. Start chat met je bot, stuur `/start`
3. Zoek `@userinfobot`, stuur `/start`, lees je chat ID

Voeg ze toe als GitHub secrets:

1. Op je repo: tabblad "Settings"
2. Linksonder "Secrets and variables" -> "Actions"
3. Klik "New repository secret"
4. Naam: `TELEGRAM_BOT_TOKEN`, value: jouw token
5. Klik "Add secret"
6. Herhaal voor `TELEGRAM_CHAT_ID` met jouw chat ID

Vanaf nu krijg je elke trade-open en trade-close per Telegram bericht.

### 6. Verifieren dat het werkt

1. Ga naar Actions tab
2. Je ziet runs verschijnen elke 5 minuten met groene vinkjes
3. Klik op een run om de logs te zien
4. In de logs zie je het dashboard met aantal trades, P&L, etc.

Eerste signaal kan een paar uur op zich laten wachten (signaal-frequentie is laag in stille markt). Wees geduldig.

### 7. State checken zonder logs te openen

Het bestand `paper_state.json` in je repo wordt na elke run geupdate met:
- Open trades per variant
- Closed trades log
- Last signal timestamps

Je kunt op elke moment direct in GitHub naar dat bestand kijken voor live status.

## Belangrijke notes

- **Vrijdag uitzettingen**: GitHub kan workflows tijdelijk pauzeren bij hoge load. Niet vaak, maar mogelijk.
- **60-dagen regel**: als de repo 60 dagen geen activiteit heeft, worden scheduled workflows uitgeschakeld. Maar omdat ONZE workflow elke 5 min een state-commit doet, blijft de repo altijd actief. Geen probleem.
- **Wijzigingen in config**: wil je later een variant aanpassen, edit `paper_trader.py` direct op GitHub of via git. Volgende cyclus gebruikt de nieuwe config.
- **Stoppen**: ga naar Settings tab -> Actions -> General -> "Disable Actions for this repository". Workflow stopt direct.
- **State resetten**: verwijder `paper_state.json` uit de repo, volgende run begint vers.

## Wat NIET in de repo moet

Per `.gitignore` worden deze al uitgesloten:
- `__pycache__/` (Python cache)
- `.venv/`, `venv/` (virtual envs)
- `.env` files (zou secrets kunnen bevatten)

Voeg nooit toe: wallet keys, private API tokens, eigen API endpoints met auth.
