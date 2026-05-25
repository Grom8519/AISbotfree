from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_path: str
    default_interval_minutes: int
    min_interval_minutes: int
    observer_lat: float | None
    observer_lon: float | None
    ais_provider: str
    initial_lookup_seconds: int
    stale_position_minutes: int
    stale_refresh_interval_minutes: int
    aisstream_api_key: str
    aisstream_wait_seconds: int
    marinetraffic_api_key: str
    marinetraffic_timespan_minutes: int
    vesselfinder_api_key: str
    vesselfinder_include_satellite: bool
    vesselfinder_interval_minutes: int
    google_service_account_file: str
    google_oauth_credentials_file: str
    google_oauth_token_file: str
    google_sheet_name: str
    google_spreadsheet_id: str
    google_worksheet_name: str


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float_env(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    return float(raw)


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        database_path=os.getenv("DATABASE_PATH", "ais_alerts.sqlite3"),
        default_interval_minutes=_int_env("DEFAULT_INTERVAL_MINUTES", 5),
        min_interval_minutes=_int_env("MIN_INTERVAL_MINUTES", 1),
        observer_lat=_float_env("OBSERVER_LAT"),
        observer_lon=_float_env("OBSERVER_LON"),
        ais_provider=os.getenv("AIS_PROVIDER", "aisstream").strip().lower(),
        initial_lookup_seconds=_int_env("INITIAL_LOOKUP_SECONDS", 20),
        stale_position_minutes=_int_env("STALE_POSITION_MINUTES", 60),
        stale_refresh_interval_minutes=_int_env("STALE_REFRESH_INTERVAL_MINUTES", 30),
        aisstream_api_key=os.getenv("AISSTREAM_API_KEY", ""),
        aisstream_wait_seconds=_int_env("AISSTREAM_WAIT_SECONDS", 90),
        marinetraffic_api_key=os.getenv("MARINETRAFFIC_API_KEY", ""),
        marinetraffic_timespan_minutes=_int_env("MARINETRAFFIC_TIMESPAN_MINUTES", 1440),
        vesselfinder_api_key=os.getenv("VESSELFINDER_API_KEY", ""),
        vesselfinder_include_satellite=os.getenv("VESSELFINDER_INCLUDE_SATELLITE", "0") == "1",
        vesselfinder_interval_minutes=_int_env("VESSELFINDER_INTERVAL_MINUTES", 1440),
        google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        google_oauth_credentials_file=os.getenv("GOOGLE_OAUTH_CREDENTIALS_FILE", ""),
        google_oauth_token_file=os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "google-sheets-token.json"),
        google_sheet_name=os.getenv("GOOGLE_SHEET_NAME", "AIS"),
        google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", ""),
        google_worksheet_name=os.getenv("GOOGLE_WORKSHEET_NAME", ""),
    )
