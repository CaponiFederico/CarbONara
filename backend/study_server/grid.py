#Raccoglitore dell'intensita' della rete. E' l'unico che parla con ElectricityMaps:
#le app chiedono alla VM, e la chiave sta solo qui
#
#   ecomonitor-study-grid             storico assestato, una volta al giorno
#   ecomonitor-study-grid --live      ogni 5 minuti: intensita' attuale, previsione,
#                                     ultime ore, e lo storico delle zone nuove
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
from collections.abc import Callable, Collection, Iterable
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
#tetto per tutti i giri insieme, contato su grid_requests: le letture ogni 5 minuti
#costano circa 15 richieste l'ora per zona
REQUESTS_PER_HOUR = 240
REQUEST_TIMEOUT_SECONDS = 30
KEY_FILE_NAME = "em_api_key"
KEY_ENV_VAR = "ECOMONITOR_EM_API_KEY"
LIMIT_REACHED = "limite di richieste orario raggiunto, si riprende al prossimo giro"

HOUR = timedelta(hours=1)

#cosa si rilegge per le app, e ogni quanto
LIVE_KINDS: tuple[tuple[str, timedelta], ...] = (
    ("latest", timedelta(minutes=5)),
    ("forecast", timedelta(minutes=30)),
    ("recent", HOUR),
)
#il timer scatta ogni 5 minuti
TIMER_SLACK = timedelta(minutes=1)
RECENT_HOURS = 2 * SETTLE_HOURS + 2
DEMAND_HOURS = 24
MAX_LIVE_ZONES = 12
REFUSED_RETRY = timedelta(hours=24)
BACKFILL_DAYS = 30
RUN_LOCK_ID = 7_202_610


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


LiveFetch = Callable[[str, str], FetchResult]


class GridStore(Protocol):
    def zones(self) -> list[tuple[str, datetime, datetime]]: ...

    def known_hours(
        self, zone: str, start: datetime, end: datetime, include_missing: bool
    ) -> set[datetime]: ...

    def save(self, zone: str, points: list[GridPoint]) -> None: ...

    def requests_since(self, moment: datetime) -> int: ...

    def last_success(self, zone: str) -> datetime | None: ...

    def log_request(
        self,
        zone: str,
        start: datetime,
        end: datetime,
        status: int,
        points: int,
        kind: str = "past",
    ) -> None: ...


class LiveStore(GridStore, Protocol):
    def live_zones(self, since: datetime, limit: int) -> list[str]: ...

    def last_request(self, zone: str, kind: str) -> tuple[datetime, int] | None: ...

    def save_snapshot(self, zone: str, kind: str, points: list[GridPoint]) -> None: ...


@dataclass
class RunReport:
    requests: int = 0
    hours_saved: int = 0
    hours_unavailable: int = 0
    snapshots: int = 0
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
    only: Collection[str] | None = None,
) -> RunReport:
    report = RunReport()
    settled = hour_floor(now) - SETTLE_HOURS * HOUR
    frozen_before = settled - REVISION_HOURS * HOUR
    budget = REQUESTS_PER_HOUR - store.requests_since(now - HOUR)
    for zone, first_hour, last_hour in store.zones():
        if only is not None and zone not in only:
            continue
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
                report.stopped = LIMIT_REACHED
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


def _live_wait(every: timedelta, status: int) -> timedelta:
    if 400 <= status < 500 and status != 429:
        return REFUSED_RETRY
    return every - TIMER_SLACK


def refresh_live(
    store: LiveStore,
    fetch_live: LiveFetch,
    fetch_past: Fetch,
    now: datetime,
    *,
    dry_run: bool = False,
) -> RunReport:
    report = RunReport()
    budget = REQUESTS_PER_HOUR - store.requests_since(now - HOUR)
    for zone in store.live_zones(now - DEMAND_HOURS * HOUR, MAX_LIVE_ZONES):
        for kind, every in LIVE_KINDS:
            last = store.last_request(zone, kind)
            if last is not None and now - last[0] < _live_wait(every, last[1]):
                continue
            if dry_run:
                log.info("chiederei %s di %s", kind, zone)
                report.requests += 1
                continue
            if budget <= 0:
                report.stopped = LIMIT_REACHED
                return report
            if kind == "recent":
                start, end = hour_floor(now) - RECENT_HOURS * HOUR, now
                result = fetch_past(zone, start, end)
            else:
                start = end = now
                result = fetch_live(kind, zone)
            budget -= 1
            report.requests += 1
            store.log_request(zone, start, end, result.status, len(result.points), kind=kind)
            if result.status == 429:
                report.stopped = "ElectricityMaps ha risposto 429: troppe richieste"
                return report
            if 400 <= result.status < 500:
                log.warning("%s di %s rifiutato da ElectricityMaps (%s)", kind, zone, result.status)
                continue
            if result.status != 200:
                report.stopped = f"ElectricityMaps ha risposto {result.status}"
                return report
            store.save_snapshot(zone, kind, result.points)
            report.snapshots += 1
    return report


def snapshot_points(points: Iterable[GridPoint]) -> list[dict[str, Any]]:
    #il formato servito alle app: un'ora senza valore non c'e', non vale zero
    return [
        {"datetime": _utc_text(point.hour_start), "g_per_kwh": point.g_per_kwh}
        for point in sorted(points, key=lambda point: point.hour_start)
        if point.g_per_kwh is not None
    ]


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


def parse_points(payload: Any, key: str = "data") -> list[GridPoint]:
    items = payload.get(key, []) if isinstance(payload, dict) else []
    points = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        moment = _parse_time(item.get("datetime"))
        value: Any = item.get("carbonIntensity")
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


def _request(api_key: str, url: str) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"auth-token": api_key})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        log.warning("ElectricityMaps non raggiungibile: %s", error)
        return 0, None


def http_fetch(api_key: str, base_url: str = BASE_URL) -> Fetch:
    def fetch(zone: str, start: datetime, end: datetime) -> FetchResult:
        query = urllib.parse.urlencode(
            {"zone": zone, "start": _utc_text(start), "end": _utc_text(end)}
        )
        status, payload = _request(api_key, f"{base_url}/carbon-intensity/past-range?{query}")
        return FetchResult(status, parse_points(payload) if status == 200 else [])

    return fetch


def http_live_fetch(api_key: str, base_url: str = BASE_URL) -> LiveFetch:
    def fetch(kind: str, zone: str) -> FetchResult:
        query = urllib.parse.urlencode({"zone": zone})
        status, payload = _request(api_key, f"{base_url}/carbon-intensity/{kind}?{query}")
        if status != 200:
            return FetchResult(status, [])
        if kind == "latest":
            #un punto solo, al primo livello della risposta
            return FetchResult(status, parse_points({"data": [payload]}))
        return FetchResult(status, parse_points(payload, "forecast"))

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

    def lock(self, wait: bool) -> bool:
        #il lock vive con la connessione: si libera all'uscita del processo
        if wait:
            self._conn.execute("SELECT pg_advisory_lock(%s)", (RUN_LOCK_ID,))
            return True
        row = self._conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_ID,)).fetchone()
        return bool(row and row[0])

    def zones(self) -> list[tuple[str, datetime, datetime]]:
        return self._conn.execute(
            "SELECT zone, min(first_hour), max(last_hour) FROM ("
            " SELECT zone, min(hour_start) AS first_hour, max(hour_start) AS last_hour"
            " FROM hourly_usage GROUP BY zone"
            " UNION ALL"
            " SELECT zone, first_requested_at - %s, last_requested_at FROM grid_zone_demand"
            ") AS wanted GROUP BY zone ORDER BY zone",
            (timedelta(days=BACKFILL_DAYS),),
        ).fetchall()

    def live_zones(self, since: datetime, limit: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT d.zone FROM grid_zone_demand d WHERE d.last_requested_at >= %s "
            "ORDER BY EXISTS (SELECT 1 FROM hourly_usage u "
            "WHERE u.zone = d.zone AND u.hour_start >= %s) DESC, "
            "d.last_requested_at DESC LIMIT %s",
            (since, since - DEMAND_HOURS * HOUR, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def last_request(self, zone: str, kind: str) -> tuple[datetime, int] | None:
        row = self._conn.execute(
            "SELECT requested_at, status FROM grid_requests WHERE zone = %s AND kind = %s "
            "ORDER BY requested_at DESC LIMIT 1",
            (zone, kind),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def save_snapshot(self, zone: str, kind: str, points: list[GridPoint]) -> None:
        from psycopg.types.json import Jsonb

        self._conn.execute(
            "INSERT INTO grid_snapshots (zone, kind, points) VALUES (%s, %s, %s) "
            "ON CONFLICT (zone, kind) DO UPDATE SET points = excluded.points, fetched_at = now()",
            (zone, kind, Jsonb(snapshot_points(points))),
        )

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
            "SELECT max(requested_at) FROM grid_requests "
            "WHERE zone = %s AND status = 200 AND kind = 'past'",
            (zone,),
        ).fetchone()
        return row[0] if row else None

    def log_request(
        self,
        zone: str,
        start: datetime,
        end: datetime,
        status: int,
        points: int,
        kind: str = "past",
    ) -> None:
        self._conn.execute(
            "INSERT INTO grid_requests (zone, window_start, window_end, status, points, kind) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (zone, start, end, status, points, kind),
        )


def run_live(
    store: LiveStore,
    fetch_live: LiveFetch,
    fetch_past: Fetch,
    now: datetime,
    *,
    dry_run: bool = False,
) -> RunReport:
    report = refresh_live(store, fetch_live, fetch_past, now, dry_run=dry_run)
    if report.stopped is not None:
        return report
    #una zona appena chiesta ha subito il suo storico, senza aspettare la notte
    fresh = {zone for zone, _, _ in store.zones() if store.last_request(zone, "past") is None}
    if not fresh:
        return report
    history = collect(store, fetch_past, now, dry_run=dry_run, only=fresh)
    report.requests += history.requests
    report.hours_saved += history.hours_saved
    report.hours_unavailable += history.hours_unavailable
    report.stopped = history.stopped
    return report


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
    parser.add_argument(
        "--live", action="store_true", help="i dati per le app: attuale, previsione, ultime ore"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    key = read_api_key()
    if key is None and not args.dry_run:
        log.error("nessuna chiave ElectricityMaps: /etc/ecomonitor-study/%s", KEY_FILE_NAME)
        return 1
    store = PostgresGridStore(os.environ.get("ECOMONITOR_STUDY_DSN", "dbname=ecomonitor_study"))
    if not store.lock(wait=not args.live):
        log.info("un altro giro e' in corso, questo salta")
        return 0
    now = datetime.now(UTC)
    fetch = http_fetch(key or "")
    if args.live:
        report = run_live(store, http_live_fetch(key or ""), fetch, now, dry_run=args.dry_run)
    else:
        report = collect(
            store, fetch, now, retry_missing=args.retry_missing, dry_run=args.dry_run
        )
    log.info(
        "richieste %d, istantanee %d, ore salvate %d, ore senza dato %d",
        report.requests,
        report.snapshots,
        report.hours_saved,
        report.hours_unavailable,
    )
    if report.stopped:
        log.warning("interrotto: %s", report.stopped)
        return 0 if "limite" in report.stopped else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
