#Validazione di un invio

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA_VERSION = 1
MAX_BODY_BYTES = 256 * 1024
MAX_HOURS_PER_REQUEST = 168
MAX_PAST_DAYS = 45
MAX_CLOCK_SKEW_SECONDS = 900
MAX_COVERED_SECONDS = 3600 + 900
MAX_MEAN_POWER_W = 3000.0
MAX_G_PER_KWH = 2000.0

OS_FAMILIES = frozenset({"windows", "linux", "macos"})
DEVICE_CLASSES = frozenset({"laptop", "desktop"})

TOP_LEVEL_FIELDS = (
    "schema_version",
    "app_version",
    "os_family",
    "device_class",
    "country_code",
    "zone",
    "hours",
)
HOUR_FIELDS = (
    "hour_start",
    "covered_seconds",
    "energy_wh",
    "gco2",
    "estimated_wh",
    "battery_wh",
    "fallback_wh",
    "cpu_wh",
)
HOUR_NUMBER_FIELDS = HOUR_FIELDS[1:]

APP_VERSION_PATTERN = re.compile(r"[0-9A-Za-z.+-]{1,32}")
COUNTRY_CODE_PATTERN = re.compile(r"[A-Z]{2}")
ZONE_PATTERN = re.compile(r"[A-Z]{2}(-[A-Z0-9]{1,8}){0,3}")
HOUR_START_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z")


@dataclass(frozen=True)
class Hour:
    hour_start: datetime
    covered_seconds: float
    energy_wh: float
    gco2: float
    estimated_wh: float
    battery_wh: float
    fallback_wh: float
    cpu_wh: float


@dataclass(frozen=True)
class Submission:
    schema_version: int
    app_version: str
    os_family: str
    device_class: str
    country_code: str
    zone: str
    hours: tuple[Hour, ...]


class ValidationError(Exception):
    def __init__(self, error: str, field: str | None, index: int | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.field = field
        self.index = index

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.error, "field": self.field, "index": self.index}


def limits() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "max_body_bytes": MAX_BODY_BYTES,
        "max_hours_per_request": MAX_HOURS_PER_REQUEST,
        "max_past_days": MAX_PAST_DAYS,
    }


def _check_shape(value: dict[str, Any], allowed: tuple[str, ...], index: int | None) -> None:
    for name in value:
        if name not in allowed:
            raise ValidationError("campo sconosciuto", name, index)
    for name in allowed:
        if name not in value:
            raise ValidationError("campo mancante", name, index)


#tolleranza sulle somme in virgola mobile
def _at_most(part: float, whole: float) -> bool:
    return part <= whole * (1 + 1e-9) + 1e-9


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        #un intero che non entra in un float
        return False


def _text(body: dict[str, Any], name: str, pattern: re.Pattern[str], message: str) -> str:
    value = body[name]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValidationError(message, name)
    return value


def _choice(body: dict[str, Any], name: str, allowed: frozenset[str], message: str) -> str:
    value = body[name]
    #il tipo prima di `in`: una lista o un oggetto non si cercano in un insieme
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(message, name)
    return value


def validate_submission(body: Any, now: datetime, joined_at: datetime) -> Submission:
    if not isinstance(body, dict):
        raise ValidationError("il corpo deve essere un oggetto JSON", None)
    _check_shape(body, TOP_LEVEL_FIELDS, None)

    schema_version = body["schema_version"]
    if not _is_number(schema_version) or schema_version != SCHEMA_VERSION:
        raise ValidationError(
            f"versione dello schema non supportata, attesa {SCHEMA_VERSION}", "schema_version"
        )
    app_version = _text(body, "app_version", APP_VERSION_PATTERN, "versione dell'app non valida")
    os_family = _choice(body, "os_family", OS_FAMILIES, "sistema operativo sconosciuto")
    device_class = _choice(
        body, "device_class", DEVICE_CLASSES, "classe di dispositivo sconosciuta"
    )
    country_code = _text(body, "country_code", COUNTRY_CODE_PATTERN, "codice paese non valido")
    zone = _text(body, "zone", ZONE_PATTERN, "zona non valida")

    raw_hours = body["hours"]
    if not isinstance(raw_hours, list) or not raw_hours:
        raise ValidationError("hours deve essere un elenco non vuoto", "hours")
    if len(raw_hours) > MAX_HOURS_PER_REQUEST:
        raise ValidationError(
            f"troppe ore in un invio, al massimo {MAX_HOURS_PER_REQUEST}", "hours"
        )

    hours: list[Hour] = []
    seen: set[datetime] = set()
    for index, raw in enumerate(raw_hours):
        hour = _validate_hour(raw, index, now, joined_at)
        #due valori per la stessa ora
        if hour.hour_start in seen:
            raise ValidationError("ora ripetuta nello stesso invio", "hour_start", index)
        seen.add(hour.hour_start)
        hours.append(hour)

    return Submission(
        schema_version=SCHEMA_VERSION,
        app_version=app_version,
        os_family=os_family,
        device_class=device_class,
        country_code=country_code,
        zone=zone,
        hours=tuple(hours),
    )


def _parse_hour_start(value: Any, index: int) -> datetime:
    message = "hour_start deve essere un'ora intera in UTC, come 2026-09-14T10:00:00Z"
    if not isinstance(value, str) or HOUR_START_PATTERN.fullmatch(value) is None:
        raise ValidationError(message, "hour_start", index)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ValidationError(message, "hour_start", index) from None


def _validate_hour(raw: Any, index: int, now: datetime, joined_at: datetime) -> Hour:
    if not isinstance(raw, dict):
        raise ValidationError("ogni ora deve essere un oggetto", None, index)
    _check_shape(raw, HOUR_FIELDS, index)

    start = _parse_hour_start(raw["hour_start"], index)
    if start + timedelta(hours=1) > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise ValidationError("ora non ancora conclusa", "hour_start", index)
    if start < now - timedelta(days=MAX_PAST_DAYS):
        raise ValidationError(
            f"ora piu' vecchia di {MAX_PAST_DAYS} giorni", "hour_start", index
        )
    #si inviano solo le ore successive all'adesione
    if start < joined_at:
        raise ValidationError("ora precedente all'adesione allo studio", "hour_start", index)

    for name in HOUR_NUMBER_FIELDS:
        value = raw[name]
        if not _is_number(value) or value < 0:
            raise ValidationError("deve essere un numero finito non negativo", name, index)

    hour = Hour(start, *(float(raw[name]) for name in HOUR_NUMBER_FIELDS))

    if hour.covered_seconds <= 0 or hour.covered_seconds > MAX_COVERED_SECONDS:
        raise ValidationError(
            f"tempo osservato fuori da (0, {MAX_COVERED_SECONDS}] s", "covered_seconds", index
        )
    if hour.energy_wh * 3600 / hour.covered_seconds > MAX_MEAN_POWER_W:
        raise ValidationError(f"potenza media oltre {MAX_MEAN_POWER_W:g} W", "energy_wh", index)
    if not _at_most(hour.gco2, hour.energy_wh / 1000 * MAX_G_PER_KWH):
        raise ValidationError(
            f"emissioni oltre {MAX_G_PER_KWH:g} gCO2/kWh per l'energia dichiarata", "gco2", index
        )
    if not _at_most(hour.estimated_wh, hour.energy_wh):
        raise ValidationError(
            "energia stimata maggiore dell'energia totale", "estimated_wh", index
        )
    if not _at_most(hour.battery_wh, hour.energy_wh):
        raise ValidationError(
            "energia a batteria maggiore dell'energia totale", "battery_wh", index
        )
    if not _at_most(hour.fallback_wh, hour.battery_wh):
        raise ValidationError(
            "energia di ripiego maggiore dell'energia a batteria", "fallback_wh", index
        )
    if not _at_most(hour.cpu_wh, hour.energy_wh):
        raise ValidationError("energia della CPU maggiore dell'energia totale", "cpu_wh", index)
    return hour
