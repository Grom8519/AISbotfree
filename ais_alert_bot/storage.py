from __future__ import annotations

from dataclasses import dataclass
import sqlite3
@dataclass(frozen=True)
class Watch:
    id: int
    chat_id: int
    query_type: str
    query_value: str
    center_lat: float
    center_lon: float
    radius_km: float
    interval_minutes: int
    active: bool
    triggered: bool
    last_checked_at: str | None
    last_distance_km: float | None
    last_seen_at: str | None


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
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    triggered INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT,
                    last_distance_km REAL,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._migrate_query_type_check(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watches_active ON watches(active, triggered)")

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
                center_lat REAL NOT NULL,
                center_lon REAL NOT NULL,
                radius_km REAL NOT NULL,
                interval_minutes INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                triggered INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_distance_km REAL,
                last_seen_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO watches (
                id, chat_id, query_type, query_value, center_lat, center_lon, radius_km,
                interval_minutes, active, triggered, last_checked_at, last_distance_km,
                last_seen_at, created_at
            )
            SELECT
                id, chat_id, query_type, query_value, center_lat, center_lon, radius_km,
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
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO watches (
                    chat_id, query_type, query_value, center_lat, center_lon, radius_km, interval_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, query_type, query_value, center_lat, center_lon, radius_km, interval_minutes),
            )
            return int(cursor.lastrowid)

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
                    datetime(last_checked_at, '+' || interval_minutes || ' minutes') <= CURRENT_TIMESTAMP
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

    def mark_checked(self, watch_id: int, distance_km: float | None, seen_at: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE watches
                SET last_checked_at = CURRENT_TIMESTAMP,
                    last_distance_km = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (distance_km, seen_at, watch_id),
            )

    def mark_triggered(self, watch_id: int, distance_km: float, seen_at: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE watches
                SET active = 0,
                    triggered = 1,
                    last_checked_at = CURRENT_TIMESTAMP,
                    last_distance_km = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (distance_km, seen_at, watch_id),
            )


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        query_type=str(row["query_type"]),
        query_value=str(row["query_value"]),
        center_lat=float(row["center_lat"]),
        center_lon=float(row["center_lon"]),
        radius_km=float(row["radius_km"]),
        interval_minutes=int(row["interval_minutes"]),
        active=bool(row["active"]),
        triggered=bool(row["triggered"]),
        last_checked_at=row["last_checked_at"],
        last_distance_km=row["last_distance_km"],
        last_seen_at=row["last_seen_at"],
    )
