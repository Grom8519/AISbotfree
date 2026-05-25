from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ais_alert_bot.config import load_settings
from ais_alert_bot.sheets import load_oauth_credentials


def main() -> None:
    settings = load_settings()
    if not settings.google_oauth_credentials_file:
        raise SystemExit("GOOGLE_OAUTH_CREDENTIALS_FILE is not configured in .env")
    if not settings.google_oauth_token_file:
        raise SystemExit("GOOGLE_OAUTH_TOKEN_FILE is not configured in .env")

    load_oauth_credentials(
        settings.google_oauth_credentials_file,
        settings.google_oauth_token_file,
        allow_interactive=True,
    )
    print(f"Google Sheets OAuth token saved to {settings.google_oauth_token_file}")


if __name__ == "__main__":
    main()
