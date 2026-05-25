from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path

from .config import Settings


logger = logging.getLogger(__name__)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

HEADERS = [
    "תאריך",
    "שעה",
    "שם כלי שייט",
    "מרחק שביקשנו",
    "מרחק בו התקבל עדכון בפועל של GPS",
    "מרחק בו התקבל דיווח לפי חישוב",
    "הפרש בין המרחק המבוקש למרחק המדויק ביותר",
]


@dataclass(frozen=True)
class GoogleSheetsLogger:
    service_account_file: str
    oauth_credentials_file: str
    oauth_token_file: str
    sheet_name: str
    spreadsheet_id: str
    worksheet_name: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleSheetsLogger":
        return cls(
            settings.google_service_account_file,
            settings.google_oauth_credentials_file,
            settings.google_oauth_token_file,
            settings.google_sheet_name,
            settings.google_spreadsheet_id,
            settings.google_worksheet_name,
        )

    @property
    def enabled(self) -> bool:
        return bool((self.sheet_name or self.spreadsheet_id) and (self.service_account_file or self.oauth_credentials_file))

    def append_alert(
        self,
        event_time: datetime,
        vessel_name: str,
        requested_distance_nm: float,
        gps_distance_nm: float | None,
        calculated_distance_nm: float | None,
    ) -> None:
        if not self.enabled:
            return

        try:
            worksheet = self._worksheet()
            self._ensure_headers(worksheet)
            worksheet.append_row(
                [
                    event_time.strftime("%d/%m/%y"),
                    event_time.strftime("%H:%M"),
                    vessel_name,
                    _format_nm(requested_distance_nm),
                    _format_optional_nm(gps_distance_nm),
                    _format_optional_nm(calculated_distance_nm),
                    _format_optional_nm(
                        calculate_best_difference_nm(
                            requested_distance_nm,
                            gps_distance_nm,
                            calculated_distance_nm,
                        )
                    ),
                ],
                value_input_option="USER_ENTERED",
            )
        except Exception as exc:
            logger.warning("Google Sheets logging failed: %r (%s)", exc, type(exc).__name__)

    def _worksheet(self):
        try:
            import gspread
        except ImportError as exc:
            raise RuntimeError("Install gspread to enable Google Sheets logging") from exc

        if self.service_account_file:
            client = gspread.service_account(filename=self.service_account_file)
        else:
            client = gspread.authorize(load_oauth_credentials(
                self.oauth_credentials_file,
                self.oauth_token_file,
                allow_interactive=False,
            ))
        spreadsheet = client.open_by_key(self.spreadsheet_id) if self.spreadsheet_id else client.open(self.sheet_name)
        if self.worksheet_name:
            return spreadsheet.worksheet(self.worksheet_name)
        return spreadsheet.sheet1

    def _ensure_headers(self, worksheet) -> None:
        first_row = worksheet.row_values(1)
        if first_row[: len(HEADERS)] != HEADERS:
            worksheet.update("A1:G1", [HEADERS])


def load_oauth_credentials(credentials_file: str, token_file: str, allow_interactive: bool = False):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Install google-auth-oauthlib to enable OAuth Google Sheets logging") from exc

    token_path = Path(token_file)
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not allow_interactive:
            raise RuntimeError(
                "Google Sheets OAuth token is missing or invalid. "
                "Run scripts/authorize_google_sheets.py once."
            )
        flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
        credentials = flow.run_local_server(port=0)

    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def calculate_best_difference_nm(
    requested_distance_nm: float,
    gps_distance_nm: float | None,
    calculated_distance_nm: float | None,
) -> float | None:
    best_distance = gps_distance_nm if gps_distance_nm is not None else calculated_distance_nm
    if best_distance is None:
        return None
    return abs(requested_distance_nm - best_distance)


def _format_optional_nm(value: float | None) -> str:
    if value is None:
        return ""
    return _format_nm(value)


def _format_nm(value: float) -> str:
    return f"{value:.2f}"
