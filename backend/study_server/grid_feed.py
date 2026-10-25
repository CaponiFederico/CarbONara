#I dati di rete serviti alle app. L'API li legge dal database 

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg

LATEST_MAX_AGE = timedelta(minutes=20)
FORECAST_MAX_AGE = timedelta(hours=2)
MAX_PAST_HOURS = 31 * 24
NOTE_DEMAND = (
    "INSERT INTO grid_zone_demand (zone) VALUES (%s) "
    "ON CONFLICT (zone) DO UPDATE SET last_requested_at = now() "
    "WHERE grid_zone_demand.last_requested_at < now() - interval '10 minutes'"
)


@dataclass(frozen=True)
class Snapshot:
    #elenco di {"datetime", "g_per_kwh"}
    points: list[dict[str, Any]]
    fetched_at: datetime


class GridReader(Protocol):
    def note_demand(self, zone: str) -> None: ...

    def snapshot(self, zone: str, kind: str) -> Snapshot | None: ...

    def settled_hours(
        self, zone: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]: ...


def is_fresh(snapshot: Snapshot | None, max_age: timedelta, now: datetime) -> bool:
    return snapshot is not None and bool(snapshot.points) and now - snapshot.fetched_at <= max_age


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def past_points(
    settled: list[tuple[datetime, float]],
    recent: Snapshot | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    by_hour = {hour.astimezone(UTC): value for hour, value in settled}
    for point in recent.points if recent is not None else []:
        hour = parse_utc(point["datetime"])
        if hour is not None and start <= hour < end and hour not in by_hour:
            by_hour[hour] = point["g_per_kwh"]
    return [
        {"datetime": utc_text(hour), "g_per_kwh": value} for hour, value in sorted(by_hour.items())
    ]


class PostgresGridReader:
    #una connessione per richiesta, come PostgresStore
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def note_demand(self, zone: str) -> None:
        with self._connect() as conn:
            conn.execute(NOTE_DEMAND, (zone,))

    def snapshot(self, zone: str, kind: str) -> Snapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT points, fetched_at FROM grid_snapshots WHERE zone = %s AND kind = %s",
                (zone, kind),
            ).fetchone()
        return None if row is None else Snapshot(list(row[0]), row[1])

    def settled_hours(
        self, zone: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hour_start, g_per_kwh FROM grid_hourly "
                "WHERE zone = %s AND hour_start >= %s AND hour_start < %s "
                "AND g_per_kwh IS NOT NULL ORDER BY hour_start",
                (zone, start, end),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]
