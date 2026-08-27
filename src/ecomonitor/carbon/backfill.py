import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ecomonitor.carbon.grid_intensity import GridCarbonIntensityService
from ecomonitor.storage.db import LocalDatabase

logger = logging.getLogger(__name__)

#Riempimento dello storico di rete per le ore in cui l'app non girava
RECONSTRUCTED_SOURCE = "reconstructed_api"

#limiti dell'API
MAX_REQUEST_DAYS = 10

SECONDS_IN_HOUR = 3600


@dataclass(frozen=True)
class BackfillResult:
    hours_filled: int = 0
    hours_already_covered: int = 0
    requests: int = 0
    empty_requests: int = 0

    @property
    def wrote_anything(self) -> bool:
        return self.hours_filled > 0


def _hour_key(moment: datetime) -> datetime:
    #l'ora solare in UTC 
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    #finestre da MAX_REQUEST_DAYS
    span = timedelta(days=MAX_REQUEST_DAYS)
    windows = []
    cursor = start
    while cursor < end:
        stop = min(cursor + span, end)
        windows.append((cursor, stop))
        cursor = stop
    return windows


def backfill_grid_history(
    db: LocalDatabase,
    service: GridCarbonIntensityService,
    start: datetime,
    end: datetime,
) -> BackfillResult:
    #Scrive un campione orario per ogni ora della finestra che non ne ha gia' uno
    if end <= start or not service.has_live_provider:
        return BackfillResult()

    covered = {
        _hour_key(sample.timestamp)
        for sample in db.grid_intensity_samples_between(start, end)
    }

    filled = already = requests = empty = 0
    for window_start, window_end in _windows(start, end):
        #se la finestra e' gia' tutta coperta, la richiesta non serve
        wanted = _missing_hours(window_start, window_end, covered)
        if not wanted:
            already += _hours_in(window_start, window_end)
            continue

        requests += 1
        points = service.get_past_points(window_start, window_end)
        if not points:
            empty += 1
            continue

        for point in points:
            hour = _hour_key(point.timestamp)
            if hour in covered or not (start <= point.timestamp < end):
                already += 1
                continue
            db.record_grid_intensity(
                point.timestamp, service.zone, point.g_per_kwh, RECONSTRUCTED_SOURCE
            )
            covered.add(hour)
            filled += 1

    result = BackfillResult(
        hours_filled=filled,
        hours_already_covered=already,
        requests=requests,
        empty_requests=empty,
    )
    if result.wrote_anything:
        logger.info(
            "Storico di rete completato: %d ore ricostruite con %d richieste",
            result.hours_filled,
            result.requests,
        )
    return result


def _hours_in(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // SECONDS_IN_HOUR))


def _missing_hours(start: datetime, end: datetime, covered: set[datetime]) -> list[datetime]:
    hours = []
    cursor = _hour_key(start)
    while cursor < end:
        if cursor >= start and cursor not in covered:
            hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours
