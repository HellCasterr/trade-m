# Trade M

Trade M is a Windows-friendly, local web application that watches Indian cash-equity prices through Zerodha Kite Connect and sends browser desktop notifications after a qualifying **completed three-minute candle**.

It is an alerting tool only. It never places orders.

## Alert rule

For each stock and trading date, the user enters a percentage `P`. The program retrieves the immediately preceding trading session's final three-minute candle close `B` and calculates:

```text
upper = B × (1 + P / 100)
lower = B × (1 - P / 100)
```

A completed candle triggers a level only when the level is inside that candle's full range:

```text
candle.low <= level <= candle.high
```

This is deliberate gap behavior. If the previous close is ₹365.20 and today's percentage is 1.43%, the upper level is ₹370.42236 (displayed as ₹370.42). Opening at ₹377 does not alert while candles remain entirely above ₹370.42. If the 09:39–09:42 candle later falls through ₹370.42 and reaches about ₹369, the first alert is generated after that candle finalizes, at approximately 09:42 IST.

The default is one upper alert and one lower alert per instrument per trading session.

## What is included

- Official Zerodha login redirect and token exchange
- KiteTicker WebSocket live data with automatic reconnect
- NSE/BSE cash-equity symbol search
- Exchange-aligned three-minute OHLC aggregation from live ticks
- Exact `Decimal` threshold calculations
- Previous-session reference retrieval from Zerodha's `3minute` historical candles
- SQLite rule, alert-state, event, and deduplication storage
- Browser desktop notifications and an in-browser alert log
- A new percentage can be entered for each stock every trading day
- Windows setup and start scripts
- Unit tests and GitHub Actions

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- A Zerodha account
- A Kite Connect app with live WebSocket and historical-data access
- Chrome, Edge, or another browser supporting desktop notifications

Zerodha documents the login flow at <https://kite.trade/docs/connect/v3/user/>, WebSocket streaming at <https://kite.trade/docs/connect/v3/websocket/>, and three-minute historical candles at <https://kite.trade/docs/connect/v3/historical/>.

## Windows setup

1. Create a Kite Connect app at <https://developers.kite.trade/apps>.
2. Set its redirect URL to exactly:

   ```text
   http://127.0.0.1:8000/auth/callback
   ```

3. Download or clone this repository.
4. Double-click `setup_windows.bat`.
5. Open the newly created `.env` file in Notepad and replace these values:

   ```dotenv
   KITE_API_KEY=your_real_api_key
   KITE_API_SECRET=your_real_api_secret
   ```

6. Double-click `start_windows.bat`.
7. In the opened dashboard, select **Sign in with Zerodha** and finish login.
8. Select **Enable notifications** and approve the browser prompt.
9. Search for an exact symbol, enter today's percentage, and select **Calculate & monitor**.
10. Keep the terminal and browser page open during market hours.

Kite access tokens expire by 6 AM on the following day, so signing in is part of the daily start-up flow. Secrets in `.env` and the local SQLite database are excluded from Git.

## Development

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe run.py
```

The API documentation is available locally at <http://127.0.0.1:8000/api/docs> while the app is running.

## Operational notes

- The app uses the tick's exchange timestamp when available and falls back to local IST time.
- A candle is finalized three seconds after its aligned end time by default. Adjust `CANDLE_FINALIZATION_DELAY_SECONDS` in `.env` if required.
- No synthetic candle is created if an instrument has no ticks during a three-minute interval.
- The live access token is kept in process memory and is not written to disk.
- Rules and events are stored in `data/trade_m.db` on the local computer.
- Changing today's percentage replaces today's rule for that symbol and clears that rule's directional alert state. Existing alert history remains available.
- Historical reference lookup covers the preceding 14 calendar days and selects the latest returned three-minute candle before the current trading date, handling normal weekends and exchange holidays without weekday assumptions.

## Safety

This software provides mechanical price alerts, not investment advice. Market data and notification delivery can be delayed or unavailable. Verify any alert against your broker before acting.
