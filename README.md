# Trade M

Trade M is a Windows-friendly, local web application that watches Indian cash-equity prices through Zerodha Kite Connect, Upstox, or DhanHQ and sends browser desktop notifications after a qualifying **completed three-minute candle**.

At the top of the dashboard, Trade M also shows a daily moving percentage based on
the latest completed India VIX session:

```text
moving percentage = previous India VIX close / 8.54400
```

For example, an India VIX close of `12.990` produces `1.5203%`. The value is fetched
after a market-data provider is connected and cached for the trading day.

It is an alerting tool only. It never places orders.

## Alert rule

For each stock and trading date, the user enters a percentage `P`. The program retrieves the immediately preceding trading session's final three-minute candle close `B` and calculates:

```text
upper = B × (1 + P / 100)
lower = B × (1 - P / 100)
```

A completed candle triggers only on a directional retracement through the level:

```text
upper level: a tick-to-tick move crosses it downward
lower level: a tick-to-tick move crosses it upward
```

This is deliberate gap and direction behavior. If the previous close is ₹365.20 and today's percentage is 1.43%, the upper level is ₹370.42236 (displayed as ₹370.42). Opening at ₹377 does not alert. A later upward move through ₹370.42 also does not alert. If the 09:39–09:42 candle retraces downward through ₹370.42 and reaches about ₹369, the first alert is generated after that candle finalizes, at approximately 09:42 IST.

The default is one upper alert and one lower alert per instrument per trading session.

## What is included

- Zerodha, Upstox, and DhanHQ authentication flows
- KiteTicker, Upstox V3, and DhanHQ WebSocket live data with automatic reconnect
- Completed-candle recovery from broker history after a feed disconnect
- Stale-feed detection with visible and desktop warnings during market hours
- NSE/BSE cash-equity symbol search
- Exchange-aligned three-minute OHLC aggregation from live ticks
- Exact `Decimal` threshold calculations
- Previous-session reference retrieval from broker historical data; Dhan one-minute rows are aligned into three-minute candles
- SQLite rule, alert-state, event, and deduplication storage
- Browser desktop notifications and an in-browser alert log
- A new percentage can be entered for each stock every trading day
- Bulk addition of the official Nifty 50 constituent list
- One common Nifty percentage with editable per-stock overrides
- Custom Excel upload for monitoring a user-defined stock list
- Editable daily percentage table and individual/bulk start-pause controls
- Provider-specific WebSocket batching (Zerodha/Upstox 500, DhanHQ 100) and throttled historical requests
- Windows setup and start scripts
- Unit tests and GitHub Actions

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- An account and market-data API access from at least one supported broker: Zerodha, Upstox, or Dhan
- Chrome, Edge, or another browser supporting desktop notifications

Zerodha documents the login flow at <https://kite.trade/docs/connect/v3/user/>, WebSocket streaming at <https://kite.trade/docs/connect/v3/websocket/>, and three-minute historical candles at <https://kite.trade/docs/connect/v3/historical/>. Upstox documents its APIs at <https://upstox.com/developer/api-documentation/>. DhanHQ documents authentication at <https://dhanhq.co/docs/v2/authentication/>, its live feed at <https://dhanhq.co/docs/v2/live-market-feed/>, and historical data at <https://dhanhq.co/docs/v2/historical-data/>.

## Windows setup

1. Prepare any provider you intend to use:

   - Create a Kite Connect app at <https://developers.kite.trade/apps>.
   - Create an Upstox app at <https://account.upstox.com/developer/apps>.
   - For Dhan, note your client ID and either generate a daily 24-hour access token in Dhan Web or create Dhan app credentials.
2. For Zerodha, set the redirect URL to exactly:

   ```text
   http://127.0.0.1:8000/auth/callback
   ```

   For Upstox, set the redirect URL to exactly:

   ```text
   http://127.0.0.1:8000/auth/upstox/callback
   ```

   If using Dhan app credentials, set their redirect URL to exactly:

   ```text
   http://127.0.0.1:8000/auth/dhan/callback
   ```

3. Download or clone this repository.
4. Double-click `setup_windows.bat`.
5. Open the newly created `.env` file in Notepad and replace the credentials for the provider(s) you use:

   ```dotenv
   KITE_API_KEY=your_real_api_key
   KITE_API_SECRET=your_real_api_secret
   UPSTOX_API_KEY=your_real_api_key
   UPSTOX_API_SECRET=your_real_api_secret
   DHAN_CLIENT_ID=your_dhan_client_id
   DHAN_ACCESS_TOKEN=your_daily_access_token
   ```

   To use Dhan's consent login instead of manually pasting its daily token, leave `DHAN_ACCESS_TOKEN` empty and fill in `DHAN_API_KEY` and `DHAN_API_SECRET`.

6. Double-click `start_windows.bat`.
7. In the opened dashboard, sign in to any configured provider(s).
8. Select **Enable notifications** and approve the browser prompt.
9. Search for an exact symbol and add a rule, or use **Load Nifty 50**, apply a common percentage, optionally edit individual percentages, then select **Add all 50**.
   To monitor a custom list, select **Download sample Excel**, fill the workbook, choose a connected provider under **Upload stocks from Excel**, and select **Upload & monitor**.
10. Keep the terminal and browser page open during market hours.

Broker sessions expire, so signing in is part of the daily start-up flow. Secrets in `.env` and the local SQLite database are excluded from Git.

## Development

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe run.py
```

The API documentation is available locally at <http://127.0.0.1:8000/api/docs> while the app is running.

## Excel stock-list import

The downloadable workbook contains these columns:

| Column | Required | Accepted values |
| --- | --- | --- |
| Trading Symbol | Yes | Exact broker symbol, such as `RELIANCE`, `TCS`, or `M&M` |
| Percentage | Yes | A value greater than 0 and at most 50, such as `1.43` |
| Exchange | No | `NSE` or `BSE`; blank defaults to `NSE` |
| Enabled | No | `TRUE` or `FALSE`; blank defaults to `TRUE` |

The app accepts `.xlsx` files up to 2 MB and at most 200 populated stock rows. It validates the complete sheet before contacting the broker. Duplicate symbols, missing percentages, unsupported exchanges, and invalid percentages stop the import and identify their row numbers. After validation, broker-specific symbol or historical-data failures are reported per stock while successfully created rules begin monitoring.

## Operational notes

- New alerts use a persistent server-sent event channel instead of depending on the general dashboard refresh. An independent five-second backfill check recovers events missed during a browser or network interruption without creating duplicates.
- Dashboard requests have timeouts, so a stuck status or rule request cannot stop alert delivery. Returning to the tab, focusing the window, or reconnecting to the network triggers an immediate recovery check.
- The Browser notifications card displays the live-channel state and provides a **Test alert** button. Use it before market hours and confirm Windows displays the notification; browser permission cannot detect Windows Focus Assist or operating-system notification suppression.
- The app uses the tick's exchange timestamp when available and falls back to local IST time.
- A candle is finalized three seconds after its aligned end time by default. Adjust `CANDLE_FINALIZATION_DELAY_SECONDS` in `.env` if required.
- No synthetic candle is created if an instrument has no ticks during a three-minute interval.
- The live access token is kept in process memory and is not written to disk.
- Rules and events are stored in `data/trade_m.db` on the local computer.
- Runtime diagnostics are written to the rotating `logs/trade_m.log` file as well as the terminal.
- After a WebSocket interruption, the app requests completed candles covering the outage. Recovery is conservative because historical OHLC data cannot reconstruct the exact order of every intrabar tick.
- The dashboard marks a connected feed stale when active rules exist during market hours but no tick has arrived for 45 seconds.
- Changing today's percentage replaces today's rule for that symbol and clears that rule's directional alert state. Existing alert history remains available.
- Historical reference lookup covers the preceding 14 calendar days and selects the latest completed three-minute candle before the current trading date, handling normal weekends and exchange holidays without weekday assumptions. Dhan only exposes selected minute intervals, so the app aggregates its one-minute history into exchange-aligned three-minute groups.
- The Nifty 50 list is retrieved from the official NSE Indices constituent CSV and cached for 12 hours. A labelled packaged fallback is used if that source is temporarily unavailable.
- A rule is tied to the provider selected when it is created. Connecting multiple providers does not duplicate a stock's alert.
- DhanHQ Data APIs require the applicable Dhan market-data plan. A Dhan Web access token normally expires after 24 hours and must be replaced in `.env` before restarting the app; consent-login sessions follow Dhan's stated expiry.

## Safety

This software provides mechanical price alerts, not investment advice. Market data and notification delivery can be delayed or unavailable. Verify any alert against your broker before acting.
