#Raccoglitore dell'intensita' della rete
#
#   ecomonitor-study-grid             raccoglie
#   ecomonitor-study-grid --dry-run   mostra cosa chiederebbe senza chiamare
#   ecomonitor-study-grid --retry-missing   richiede anche le ore rimaste NULL

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("study_server.grid")

BASE_URL = "https://api.electricitymaps.com/v3"
MAX_REQUEST_HOURS = 240
SETTLE_HOURS = 24
REVISION_HOURS = MAX_REQUEST_HOURS
MIN_REFRESH_HOURS = 20
TRAILING_HOURS = 14 * 24
REQUESTS_PER_HOUR = 40
REQUEST_TIMEOUT_SECONDS = 30
KEY_FILE_NAME = "em_api_key"
KEY_ENV_VAR = "ECOMONITOR_EM_API_KEY"

HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class GridPoint:
    hour_start: datetime
    g_per_kwh: float | None
    is_estimated: bool | None = None
    estimation_method: str | None = None
    source_updated_at: datetime | None = None


@dataclass(frozen=True)
class FetchResult:
    status: int
    points: list[GridPoint]


Fetch = Callable[[str, datetime, datetime], FetchResult]


class GridStore(Protocol):
    def zones(self) -> list[tuple[str, datetime, datetime]]: ...

    def known_hours(
        self, zone: str, start: datetime, end: datetime, include_missing: bool
    ) -> set[datetime]: ...

    def save(self, zone: str, points: list[GridPoint]) -> None: ...

    def requests_since(self, moment: datetime) -> int: ...

    def last_success(self, zone: str) -> datetime | None: ...

    def log_request(
        self, zone: str, start: datetime, end: datetime, status: int, points: int
    ) -> None: ...


@dataclass
class RunReport:
    requests: int = 0
    hours_saved: int = 0
    hours_unavailable: int = 0
    stopped: str | None = None


def hour_floor(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def plan_windows(
    start: datetime, end: datetime, known: set[datetime]
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    hour = start
    while hour < end:
        if hour in known:
            hour += HOUR
            continue
        window_end = min(hour + MAX_REQUEST_HOURS * HOUR, end)
        #la finestra si chiude dopo l'ultima ora mancante che contiene
        last_missing = hour
        cursor = hour
        while cursor < window_end:
            if cursor not in known:
                last_missing = cursor
            cursor += HOUR
        windows.append((hour, last_missing + HOUR))
        hour = window_end
    return windows


def collect(
    store: GridStore,
    fetch: Fetch,
    now: datetime,
    *,
    retry_missing: bool = False,
    dry_run: bool = False,
) -> RunReport:
    report = RunReport()
    settled = hour_floor(now) - SETTLE_HOURS * HOUR
    frozen_before = settled - REVISION_HOURS * HOUR
    budget = REQUESTS_PER_HOUR - store.requests_since(now - HOUR)
    for zone, first_hour, last_hour in store.zones():
        start = hour_floor(first_hour)
        end = min(settled, hour_floor(last_hour) + TRAILING_HOURS * HOUR)
        if start >= end:
            continue
        known = store.known_hours(zone, start, end, include_missing=not retry_missing)
        last = store.last_success(zone)
        if last is None or now - last >= MIN_REFRESH_HOURS * HOUR:
            known = {hour for hour in known if hour < frozen_before}
        else:
            hour = max(start, frozen_before)
            while hour < end:
                known.add(hour)
                hour += HOUR
        for window_start, window_end in plan_windows(start, end, known):
            if dry_run:
                log.info("chiederei %s da %s a %s", zone, window_start, window_end)
                report.requests += 1
                continue
            if budget <= 0:
                report.stopped = "limite di richieste orario raggiunto, si riprende al prossimo giro"
                return report
            result = fetch(zone, window_start, window_end)
            budget -= 1
            report.requests += 1
            store.log_request(zone, window_start, window_end, result.status, len(result.points))
            if result.status == 429:
                report.stopped = "ElectricityMaps ha risposto 429: troppe richieste"
                return report
            if result.status in (401, 403):
                log.warning("zona %s non accessibile con questa chiave (%s)", zone, result.status)
                break
            if result.status != 200:
                report.stopped = f"ElectricityMaps ha risposto {result.status}"
                return report
            points = _fill_window(window_start, window_end, known, result.points)
            store.save(zone, points)
            report.hours_saved += sum(p.g_per_kwh is not None for p in points)
            report.hours_unavailable += sum(p.g_per_kwh is None for p in points)
    return report


def _fill_window(
    start: datetime,
    end: datetime,
    known: set[datetime],
    received: Iterable[GridPoint],
) -> list[GridPoint]:
    by_hour = {
        hour_floor(point.hour_start): point
        for point in received
        if start <= point.hour_start < end
    }
    result = []
    hour = start
    while hour < end:
        if hour not in known:
            result.append(by_hour.get(hour, GridPoint(hour, None)))
        hour += HOUR
    return result


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def parse_points(payload: Any) -> list[GridPoint]:
    points = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        moment = _parse_time(item.get("datetime"))
        value = item.get("carbonIntensity")
        if moment is None:
            continue
        valid = isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
        points.append(
            GridPoint(
                hour_start=hour_floor(moment),
                g_per_kwh=float(value) if valid else None,
                is_estimated=item.get("isEstimated"),
                estimation_method=item.get("estimationMethod"),
                source_updated_at=_parse_time(item.get("updatedAt")),
            )
        )
    return points


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_fetch(api_key: str, base_url: str = BASE_URL) -> Fetch:
    def fetch(zone: str, start: datetime, end: datetime) -> FetchResult:
        query = urllib.parse.urlencode(
            {"zone": zone, "start": _utc_text(start), "end": _utc_text(end)}
        )
        request = urllib.request.Request(
            f"{base_url}/carbon-intensity/past-range?{query}",
            headers={"auth-token": api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return FetchResult(response.status, parse_points(json.load(response)))
        except urllib.error.HTTPError as error:
            return FetchResult(error.code, [])
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            log.warning("ElectricityMaps non raggiungibile: %s", error)
            return FetchResult(0, [])

    return fetch


UPSERT_GRID_HOUR = """
INSERT INTO grid_hourly (zone, hour_start, g_per_kwh, first_g_per_kwh, is_estimated,
                         estimation_method, source_updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (zone, hour_start) DO UPDATE SET
    g_per_kwh = coalesce(excluded.g_per_kwh, grid_hourly.g_per_kwh),
    first_g_per_kwh = coalesce(grid_hourly.first_g_per_kwh, excluded.g_per_kwh),
    revisions = grid_hourly.revisions + (
        grid_hourly.g_per_kwh IS NOT NULL
        AND excluded.g_per_kwh IS NOT NULL
        AND grid_hourly.g_per_kwh <> excluded.g_per_kwh
    )::int,
    is_estimated = coalesce(excluded.is_estimated, grid_hourly.is_estimated),
    estimation_method = CASE WHEN excluded.g_per_kwh IS NULL
        THEN grid_hourly.estimation_method ELSE excluded.estimation_method END,
    source_updated_at = coalesce(excluded.source_updated_at, grid_hourly.source_updated_at),
    fetched_at = now()
"""


class PostgresGridStore:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)

    def zones(self) -> list[tuple[str, datetime, datetime]]:
        return self._conn.execute(
            "SELECT zone, min(hour_start), max(hour_start) FROM hourly_usage "
            "GROUP BY zone ORDER BY zone"
        ).fetchall()

    def known_hours(
        self, zone: str, start: datetime, end: datetime, include_missing: bool
    ) -> set[datetime]:
        condition = "" if include_missing else " AND g_per_kwh IS NOT NULL"
        rows = self._conn.execute(
            "SELECT hour_start FROM grid_hourly "
            "WHERE zone = %s AND hour_start >= %s AND hour_start < %s" + condition,
            (zone, start, end),
        ).fetchall()
        return {row[0].astimezone(UTC) for row in rows}

    def save(self, zone: str, points: list[GridPoint]) -> None:
        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany(
                UPSERT_GRID_HOUR,
                [
                    (
                        zone,
                        p.hour_start,
                        p.g_per_kwh,
                        p.g_per_kwh,
                        p.is_estimated,
                        p.estimation_method,
                        p.source_updated_at,
                    )
                    for p in points
                ],
            )

    def requests_since(self, moment: datetime) -> int:
        row = self._conn.execute(
            "SELECT count(*) FROM grid_requests WHERE requested_at >= %s", (moment,)
        ).fetchone()
        return int(row[0]) if row else 0

    def last_success(self, zone: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT max(requested_at) FROM grid_requests WHERE zone = %s AND status = 200",
            (zone,),
        ).fetchone()
        return row[0] if row else None

    def log_request(
        self, zone: str, start: datetime, end: datetime, status: int, points: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO grid_requests (zone, window_start, window_end, status, points) "
            "VALUES (%s, %s, %s, %s, %s)",
            (zone, start, end, status, points),
        )


def read_api_key() -> str | None:
    #systemd la passa con LoadCredential: leggibile solo da questo servizio
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials:
        path = Path(credentials) / KEY_FILE_NAME
        if path.is_file():
            return path.read_text(encoding="utf-8-sig").strip() or None
    return os.environ.get(KEY_ENV_VAR, "").strip() or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecomonitor-study-grid")
    parser.add_argument("--dry-run", action="store_true", help="mostra le richieste, non le fa")
    parser.add_argument(
        "--retry-missing", action="store_true", help="richiede anche le ore rimaste senza dato"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    key = read_api_key()
    if key is None and not args.dry_run:
        log.error("nessuna chiave ElectricityMaps: /etc/ecomonitor-study/%s", KEY_FILE_NAME)
        return 1
    store = PostgresGridStore(os.environ.get("ECOMONITOR_STUDY_DSN", "dbname=ecomonitor_study"))
    report = collect(
        store,
        http_fetch(key or ""),
        datetime.now(UTC),
        retry_missing=args.retry_missing,
        dry_run=args.dry_run,
    )
    log.info(
        "richieste %d, ore salvate %d, ore senza dato %d",
        report.requests,
        report.hours_saved,
        report.hours_unavailable,
    )
    if report.stopped:
        log.warning("interrotto: %s", report.stopped)
        return 0 if "limite" in report.stopped else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
