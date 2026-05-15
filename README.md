# AIS Telegram Alert Bot

Telegram bot that watches AIS vessel positions and sends an alert when a vessel enters a configured radius around you.

## Features

- Dialog flow after `/start`.
- Watch a vessel by MMSI.
- Watch by IMO when the selected AIS provider supports it.
- Radius in nautical miles.
- Free AISStream provider support.
- Store watches in SQLite.
- Periodic polling with per-watch intervals.
- One alert per approach, then tracking stops automatically.

## Telegram Commands

```text
/start
/new
/watch mmsi <mmsi> <lat> <lon> <radius_km> [interval_min]
/list
/remove
/reset
/empty_list
```

Main user flow:

```text
/start
Bot: Enter MMSI or IMO
User: 538003913
Bot: How many nautical miles from you should I alert at?
User: 3.5
Bot: If OBSERVER_LAT/OBSERVER_LON are not configured, asks for Telegram location.
```

Current bot menu:

```text
/start
/list
/remove
/reset
/empty_list
```

`/remove` removes an active watch by MMSI or IMO. `/reset` cancels all active watches after a 3-digit confirmation code. `/empty_list` deletes inactive watch records after a 5-digit confirmation code.

## Setup

1. Create a bot with BotFather and copy the Telegram token.
2. Choose an AIS provider and obtain an API key.
3. Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in the values.
5. Run the bot:

```powershell
python -m ais_alert_bot.bot
```

## AIS Providers

Set `AIS_PROVIDER` in `.env`.

### AISStream, free option

```env
AIS_PROVIDER=aisstream
AISSTREAM_API_KEY=your_free_key
AISSTREAM_WAIT_SECONDS=90
```

AISStream streams global AIS data over WebSocket and supports MMSI filters. IMO support is best-effort: the bot first waits for `ShipStaticData` with the requested IMO to discover MMSI, then tracks that MMSI. For reliable free tracking, use MMSI whenever possible.

### MarineTraffic

```env
AIS_PROVIDER=marinetraffic
MARINETRAFFIC_API_KEY=your_key
```

MMSI tracking uses:

```text
https://services.marinetraffic.com/api/exportvessel/{api_key}?v=6&mmsi=<mmsi>&protocol=jsono
```

Name tracking is intentionally not enabled for MarineTraffic by default because broad name scans can be expensive and contract-dependent. Prefer resolving the vessel name to MMSI once, then use `/watch mmsi`.

### VesselFinder

```env
AIS_PROVIDER=vesselfinder
VESSELFINDER_API_KEY=your_key
```

MMSI tracking uses:

```text
https://api.vesselfinder.com/vessels?userkey=<key>&mmsi=<mmsi>&format=json
```

Name tracking uses `vesselslist`, so the vessel must be in the predefined fleet for that VesselFinder account.

## Notes

- AIS data may be delayed, especially outside terrestrial receiver coverage.
- Polling too often can consume credits quickly. Start with 5-15 minute intervals unless you have a high-volume plan.
- The bot calculates distance with the haversine formula from the latest AIS coordinate to your configured or shared Telegram location.
- Telegram does not expose your location automatically. Either set `OBSERVER_LAT` / `OBSERVER_LON` in `.env`, or send location when the bot asks.
