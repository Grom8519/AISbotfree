from __future__ import annotations

from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class Watch:
    id: int
    chat_id: int
    query_type: str
    query_value: str
    vessel_name: str | None
    center_lat: float
    center_lon: float
    radius_km: float
    interval_minutes: int
    active: bool
    triggered: bool
    last_checked_at: str | None
    last_distance_km: float | None
    last_seen_at: str | None
    predicted_alert_at: str | None
    last_speed_knots: float | None
    last_course: float | None
    eta_unavailable_notified: bool
    calculation_alert_sent: bool
    last_position_stale: bool


class WatchStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    query_type TEXT NOT NULL CHECK(query_type IN ('mmsi', 'imo', 'name')),
                    query_value TEXT NOT NULL,
                    vessel_name TEXT,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    triggered INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT,
                    last_distance_km REAL,
                    last_seen_at TEXT,
                    predicted_alert_at TEXT,
                    last_speed_knots REAL,
                    last_course REAL,
                    eta_unavailable_notified INTEGER NOT NULL DEFAULT 0,
                    calculation_alert_sent INTEGER NOT NULL DEFAULT 0,
                    last_position_stale INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._migrate_query_type_check(conn)
            self._ensure_column(conn, "vessel_name", "TEXT")
            self._ensure_column(conn, "predicted_alert_at", "TEXT")
            self._ensure_column(conn, "last_speed_knots", "REAL")
            self._ensure_column(conn, "last_course", "REAL")
            self._ensure_column(conn, "eta_unavailable_notified", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "calculation_alert_sent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "last_position_stale", "INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watches_active ON watches(active, triggered)")

    def _ensure_column(self, conn: sqlite3.Connection, name: str, definition: str) -> None:
        rows = conn.execute("PRAGMA table_info(watches)").fetchall()
        if name not in {row["name"] for row in rows}:
            conn.execute(f"ALTER TABLE watches ADD COLUMN {name} {definition}")

    def _migrate_query_type_check(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'watches'").fetchone()
        if not row or "query_type IN ('mmsi', 'name')" not in row["sql"]:
            return
        conn.execute("ALTER TABLE watches RENAME TO watches_old")
        conn.execute(
            """
            CREATE TABLE watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                query_type TEXT NOT NULL CHECK(query_type IN ('mmsi', 'imo', 'name')),
                query_value TEXT NOT NULL,
                vessel_name TEXT,
                center_lat REAL NOT NULL,
                center_lon REAL NOT NULL,
                radius_km REAL NOT NULL,
                interval_minutes INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                triggered INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_distance_km REAL,
                last_seen_at TEXT,
                predicted_alert_at TEXT,
                last_speed_knots REAL,
                last_course REAL,
                eta_unavailable_notified INTEGER NOT NULL DEFAULT 0,
                calculation_alert_sent INTEGER NOT NULL DEFAULT 0,
                last_position_stale INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO watches (
                id, chat_id, query_type, query_value, vessel_name, center_lat, center_lon, radius_km,
                interval_minutes, active, triggered, last_checked_at, last_distance_km,
                last_seen_at, created_at
            )
            SELECT
                id, chat_id, query_type, query_value, NULL, center_lat, center_lon, radius_km,
                interval_minutes, active, triggered, last_checked_at, last_distance_km,
                last_seen_at, created_at
            FROM watches_old
            """
        )
        conn.execute("DROP TABLE watches_old")

    def add_watch(
        self,
        chat_id: int,
        query_type: str,
        query_value: str,
        center_lat: float,
        center_lon: float,
        radius_km: float,
        interval_minutes: int,
        vessel_name: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO watches (
                    chat_id, query_type, query_value, vessel_name, center_lat, center_lon, radius_km, interval_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, query_type, query_value, vessel_name, center_lat, center_lon, radius_km, interval_minutes),
            )
            return int(cursor.lastrowid)

    def latest_vessel_name(self, query_value: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT vessel_name
                FROM watches
                WHERE query_value = ?
                  AND vessel_name IS NOT NULL
                  AND vessel_name != ''
                ORDER BY COALESCE(last_checked_at, created_at) DESC
                LIMIT 1
                """,
                (query_value,),
            ).fetchone()
        return None if row is None else row["vessel_name"]

    def list_for_chat(self, chat_id: int) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watches WHERE chat_id = ? ORDER BY id DESC",
                (chat_id,),
            ).fetchall()
        return [_row_to_watch(row) for row in rows]

    def due_watches(self) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM watches
                WHERE active = 1
                  AND triggered = 0
                  AND (
                    last_checked_at IS NULL OR
                    datetime(last_checked_at, '+' || interval_minutes || ' minutes') <= CURRENT_TIMESTAMP OR
                    (predicted_alert_at IS NOT NULL AND datetime(predicted_alert_at) <= CURRENT_TIMESTAMP) OR
                    (
                      last_distance_km IS NOT NULL AND
                      last_position_stale = 0 AND
                      last_distance_km <= radius_km + 3.704 AND
                      datetime(last_checked_at, '+1 minutes') <= CURRENT_TIMESTAMP
                    )
                  )
                ORDER BY COALESCE(last_checked_at, '1970-01-01')
                """
            ).fetchall()
        return [_row_to_watch(row) for row in rows]

    def remove(self, chat_id: int, watch_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE watches SET active = 0 WHERE chat_id = ? AND id = ?",
                (chat_id, watch_id),
            )
            return cursor.rowcount > 0

    def remove_by_vessel(self, chat_id: int, query_value: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE watches
                SET active = 0
                WHERE chat_id = ?
                  AND active = 1
                  AND triggered = 0
                  AND query_value = ?
                """,
                (chat_id, query_value),
            )
            return cursor.rowcount

    def has_active_vessel(self, chat_id: int, query_value: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM watches
                WHERE chat_id = ?
                  AND active = 1
                  AND triggered = 0
                  AND query_value = ?
                LIMIT 1
                """,
                (chat_id, query_value),
            ).fetchone()
            return row is not None

    def stop_all_active(self, chat_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE watches
                SET active = 0
                WHERE chat_id = ?
                  AND active = 1
                  AND triggered = 0
                """,
                (chat_id,),
            )
            return cursor.rowcount

    def delete_inactive(self, chat_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM watches
                WHERE chat_id = ?
                  AND (
                    active = 0 OR triggered = 1
                  )
                """,
                (chat_id,),
            )
            return cursor.rowcount

    def reset(self, chat_id: int, watch_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE watches SET active = 1, triggered = 0 WHERE chat_id = ? AND id = ?",
                (chat_id, watch_id),
            )
            return cursor.rowcount > 0

    def mark_checked(
        self,
        watch_id: int,
        distance_km: float | None,
        seen_at: str | None,
        predicted_alert_at: str | None = None,
        speed_knots: float | None = None,
        course: float | None = None,
        eta_unavailable_notified: bool | None = None,
        calculation_alert_sent: bool | None = None,
        position_stale: bool | None = None,
        vessel_name: str | None = None,
    ) -> None:
        updates = [
            "last_checked_at = CURRENT_TIMESTAMP",
            "last_distance_km = ?",
            "last_seen_at = ?",
            "predicted_alert_at = ?",
            "last_speed_knots = ?",
            "last_course = ?",
        ]
        values: list[object] = [distance_km, seen_at, predicted_alert_at, speed_knots, course]
        if eta_unavailable_notified is not None:
            updates.append("eta_unavailable_notified = ?")
            values.append(1 if eta_unavailable_notified else 0)
        if calculation_alert_sent is not None:
            updates.append("calculation_alert_sent = ?")
            values.append(1 if calculation_alert_sent else 0)
        if position_stale is not None:
            updates.append("last_position_stale = ?")
            values.append(1 if position_stale else 0)
        if vessel_name:
            updates.append("vessel_name = ?")
            values.append(vessel_name)
        values.append(watch_id)

        with self._connect() as conn:
            conn.execute(
                f"UPDATE watches SET {', '.join(updates)} WHERE id = ?",
                values,
            )

    def set_interval(self, watch_id: int, interval_minutes: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watches SET interval_minutes = ? WHERE id = ?",
                (interval_minutes, watch_id),
            )

    def mark_triggered(
        self,
        watch_id: int,
        distance_km: float,
        seen_at: str | None,
        vessel_name: str | None = None,
    ) -> None:
        name_update = ", vessel_name = ?" if vessel_name else ""
        values: list[object] = [distance_km, seen_at]
        if vessel_name:
            values.append(vessel_name)
        values.append(watch_id)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE watches
                SET active = 0,
                    triggered = 1,
                    last_checked_at = CURRENT_TIMESTAMP,
                    last_distance_km = ?,
                    last_seen_at = ?,
                    predicted_alert_at = NULL,
                    last_position_stale = 0
                    {name_update}
                WHERE id = ?
                """,
                values,
            )


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        query_type=str(row["query_type"]),
        query_value=str(row["query_value"]),
        vessel_name=row["vessel_name"],
        center_lat=float(row["center_lat"]),
        center_lon=float(row["center_lon"]),
        radius_km=float(row["radius_km"]),
        interval_minutes=int(row["interval_minutes"]),
        active=bool(row["active"]),
        triggered=bool(row["triggered"]),
        last_checked_at=row["last_checked_at"],
        last_distance_km=row["last_distance_km"],
        last_seen_at=row["last_seen_at"],
        predicted_alert_at=row["predicted_alert_at"],
        last_speed_knots=row["last_speed_knots"],
        last_course=row["last_course"],
        eta_unavailable_notified=bool(row["eta_unavailable_notified"]),
        calculation_alert_sent=bool(row["calculation_alert_sent"]),
        last_position_stale=bool(row["last_position_stale"]),
    )
