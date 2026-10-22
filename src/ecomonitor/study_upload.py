import json
import logging
import platform
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

import ecomonitor
from ecomonitor.carbon.attribution import AttributionSource
from ecomonitor.history import covered_seconds_per_row
from ecomonitor.storage.db import EnergySample, LocalDatabase
from ecomonitor.study_phase import StudyDates, StudyPhase, StudyStartFailed

#Invio degli aggregati orari al server dello studio.


logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
STATUS_PATH = "/v1/status"
HOURS_PATH = "/v1/hours"
START_PATH = "/v1/start"
INSTALL_ID_HEADER = "X-Install-Id"

UPLOAD_INTERVAL_SECONDS = 3600.0
#dopo un errore di rete si riprova prima dell'ora successiva
RETRY_INTERVAL_SECONDS = 600.0

#un'ora si invia solo quando le sue righe sono sicuramente su disco
MIN_SETTLE_SECONDS = 600.0

REQUEST_TIMEOUT_SECONDS = 15.0

OS_FAMILIES = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}

BATTERY_SOURCES = frozenset(
    {
        AttributionSource.BATTERY_POOL.value,
        AttributionSource.BATTERY_POOL_PARTIAL.value,
        AttributionSource.BATTERY_POOL_FALLBACK.value,
    }
)
#il ripiego e' una parte dell'energia a batteria
FALLBACK_SOURCE = AttributionSource.BATTERY_POOL_FALLBACK.value

#i livelli di misura di MeasurementMode.kind, ognuno col suo campo
LEVEL_FIELDS = {
    "native": "native_wh",
    "partial": "partial_wh",
    "calibrated": "calibrated_wh",
    "generic": "generic_wh",
}


class UploadStopped(Exception):
    #il giro finisce qui;
    def __init__(self, reason: str, retry: bool, token_rejected: bool = False) -> None:
        super().__init__(reason)
        self.retry = retry
        #401
        self.token_rejected = token_rejected


@dataclass
class HourlyUsage:
    hour_start: datetime
    zone: str = ""
    covered_seconds: float = 0.0
    energy_wh: float = 0.0
    gco2: float = 0.0
    estimated_wh: float = 0.0
    battery_wh: float = 0.0
    fallback_wh: float = 0.0
    cpu_wh: float = 0.0
    #fase dell'ora: quella con piu' secondi coperti, None senza studio
    phase: str | None = None
    dashboard_visible_seconds: float = 0.0
    advice_wait_seconds: float = 0.0
    advice_now_seconds: float = 0.0
    plugged_seconds: float = 0.0
    charge_percent: float = 0.0
    native_wh: float = 0.0
    partial_wh: float = 0.0
    calibrated_wh: float = 0.0
    generic_wh: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "hour_start": self.hour_start.strftime("%Y-%m-%dT%H:00:00Z"),
            "phase": self.phase,
            "covered_seconds": self.covered_seconds,
            "energy_wh": self.energy_wh,
            "gco2": self.gco2,
            "estimated_wh": self.estimated_wh,
            "battery_wh": self.battery_wh,
            "fallback_wh": self.fallback_wh,
            "cpu_wh": self.cpu_wh,
            "dashboard_visible_seconds": self.dashboard_visible_seconds,
            "advice_wait_seconds": self.advice_wait_seconds,
            "advice_now_seconds": self.advice_now_seconds,
            "plugged_seconds": self.plugged_seconds,
            "charge_percent": self.charge_percent,
            "native_wh": self.native_wh,
            "partial_wh": self.partial_wh,
            "calibrated_wh": self.calibrated_wh,
            "generic_wh": self.generic_wh,
        }


@dataclass(frozen=True)
class ServerStatus:
    joined_at: datetime
    last_hour_start: datetime | None
    max_hours_per_request: int
    max_body_bytes: int
    max_past_days: int
    phase: str = "baseline"
    study: StudyDates = field(default_factory=lambda: StudyDates(None, None))


@dataclass
class UploadReport:
    accepted: int = 0
    rejected: list[datetime] = field(default_factory=list)
    #il calendario letto dal server in questo giro
    study: StudyDates | None = None
    #studio finito e niente piu' da inviare
    finished: bool = False


def hour_floor(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hour_ceil(moment: datetime) -> datetime:
    floor = hour_floor(moment)
    return floor if floor == moment.astimezone(UTC) else floor + timedelta(hours=1)


def settle_seconds_for(flush_interval_seconds: float) -> float:
    return max(MIN_SETTLE_SECONDS, 2 * flush_interval_seconds)


def aggregate_hours(
    samples: list[EnergySample],
    zone_samples: Iterable[tuple[datetime, str]],
    current_zone: str,
) -> list[HourlyUsage]:
    #righe al minuto sommate per ora UTC, in ordine di tempo
    hours: dict[datetime, HourlyUsage] = {}
    #secondi coperti per fase, in ogni ora
    phases: dict[datetime, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))
    for sample, seconds in zip(samples, covered_seconds_per_row(samples), strict=True):
        start = hour_floor(sample.timestamp)
        hour = hours.setdefault(start, HourlyUsage(start))
        hour.covered_seconds += seconds
        hour.energy_wh += sample.wh
        hour.gco2 += sample.gco2
        hour.cpu_wh += sample.cpu_wh
        if sample.is_estimated:
            hour.estimated_wh += sample.wh
        if sample.attribution_source in BATTERY_SOURCES:
            hour.battery_wh += sample.wh
        if sample.attribution_source == FALLBACK_SOURCE:
            hour.fallback_wh += sample.wh
        if sample.is_plugged:
            hour.plugged_seconds += seconds
        hour.charge_percent += sample.charged_percent
        #i secondi registrati dal tick, mai oltre il tempo coperto della riga
        hour.dashboard_visible_seconds += min(sample.dashboard_visible_seconds, seconds)
        wait = min(sample.advice_wait_seconds, seconds)
        hour.advice_wait_seconds += wait
        hour.advice_now_seconds += min(sample.advice_now_seconds, seconds - wait)
        #una riga senza livello resta fuori
        level = LEVEL_FIELDS.get(sample.measurement_kind or "")
        if level is not None:
            setattr(hour, level, getattr(hour, level) + sample.wh)
        phases[start][sample.study_phase] += seconds

    zones: dict[datetime, Counter[str]] = defaultdict(Counter)
    for moment, zone in zone_samples:
        zones[hour_floor(moment)][zone] += 1

    result = []
    for start in sorted(hours):
        hour = hours[start]
        if hour.covered_seconds <= 0:
            continue
        counts = zones.get(start)
        hour.zone = counts.most_common(1)[0][0] if counts else current_zone
        #il cambio di fase cade a pochi secondi dall'ora intera
        seconds_by_phase = phases[start]
        hour.phase = max(seconds_by_phase, key=seconds_by_phase.__getitem__)
        result.append(hour)
    return result


def zone_runs(hours: list[HourlyUsage]) -> list[list[HourlyUsage]]:
    #il server vuole una zona per invio
    runs: list[list[HourlyUsage]] = []
    for hour in hours:
        if runs and runs[-1][-1].zone == hour.zone:
            runs[-1].append(hour)
        else:
            runs.append([hour])
    return runs


def detect_os_family() -> str | None:
    return OS_FAMILIES.get(platform.system())


def detect_device_class() -> str:
    from ecomonitor.power_context import read_power_source_state

    return "desktop" if read_power_source_state().battery_percent is None else "laptop"


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"data non valida: {value!r}")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"data senza fuso: {value!r}")
    return moment.astimezone(UTC)


def _positive_int(limits: dict[str, Any], name: str) -> int:
    value = limits.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TypeError(f"limite {name} non valido: {value!r}")
    return value


def parse_status(payload: object) -> ServerStatus:
    if not isinstance(payload, dict) or not isinstance(payload.get("limits"), dict):
        raise TypeError("risposta di stato senza limits")
    limits = payload["limits"]
    if limits.get("schema_version") != SCHEMA_VERSION:
        raise UploadStopped(
            f"il server vuole lo schema {limits.get('schema_version')!r}, questa versione "
            f"dell'app scrive il {SCHEMA_VERSION}: serve aggiornare l'app",
            retry=False,
        )
    last = payload.get("last_hour_start")
    phase, study = parse_study(payload.get("study"))
    return ServerStatus(
        joined_at=_parse_time(payload.get("joined_at")),
        last_hour_start=None if last is None else _parse_time(last),
        max_hours_per_request=_positive_int(limits, "max_hours_per_request"),
        max_body_bytes=_positive_int(limits, "max_body_bytes"),
        max_past_days=_positive_int(limits, "max_past_days"),
        phase=phase,
        study=study,
    )


def parse_study(value: object) -> tuple[str, StudyDates]:
    #la fase e il calendario, come li scrivono /v1/status e /v1/hours
    if not isinstance(value, dict):
        raise TypeError("risposta senza study")
    phase = value.get("phase")
    if phase not in {item.value for item in StudyPhase}:
        raise ValueError(f"fase sconosciuta: {phase!r}")
    return phase, StudyDates(
        advice_starts_at=_optional_time(value.get("advice_starts_at")),
        ends_at=_optional_time(value.get("ends_at")),
        starts_at=_optional_time(value.get("starts_at")),
    )


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _rejection(response: httpx.Response) -> tuple[str, int | None]:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200], None
    if not isinstance(payload, dict):
        return str(payload)[:200], None
    index = payload.get("index")
    #bool e' un int per Python, e True varrebbe l'ora 1
    if not isinstance(index, int) or isinstance(index, bool):
        index = None
    return str(payload.get("error")), index


def start_study(
    endpoint: str, token: str, *, transport: httpx.BaseTransport | None = None
) -> StudyDates:
    #verifica il codice appena scritto e riceve il calendario
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(
                endpoint.rstrip("/") + START_PATH,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Studio: verifica del codice non riuscita (%s)", exc)
        raise StudyStartFailed(
            "Per verificare il codice serve la connessione a Internet. Collegati e riprova."
        ) from exc
    if response.status_code == 401:
        raise StudyStartFailed(
            "Il server dello studio non riconosce questo codice: controlla di averlo "
            "scritto per intero, o chiedine uno nuovo.",
            rejected=True,
        )
    if response.status_code != 200:
        raise StudyStartFailed(
            f"Il server dello studio non risponde come dovrebbe ({response.status_code}): "
            "riprova fra qualche minuto."
        )
    try:
        _phase, dates = parse_study(response.json().get("study"))
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("Studio: risposta alla verifica illeggibile (%s)", exc)
        raise StudyStartFailed(
            "La risposta del server dello studio non si legge: serve aggiornare l'app."
        ) from exc
    if dates.starts_at is None:
        raise StudyStartFailed(
            "Il server dello studio non ha fissato l'inizio: serve aggiornare il server."
        )
    logger.info("Studio: codice verificato, si parte il %s", dates.starts_at.isoformat())
    return dates


class StudyUploader:
    def __init__(
        self,
        db: LocalDatabase,
        endpoint: str,
        token: str,
        *,
        install_id: str,
        os_family: str,
        device_class: str,
        settle_seconds: float = MIN_SETTLE_SECONDS,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._endpoint = endpoint.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._status_headers = {**self._headers, INSTALL_ID_HEADER: install_id}
        self._install_id = install_id
        self._os_family = os_family
        self._device_class = device_class
        self._settle_seconds = settle_seconds
        self._transport = transport
        self._now = now or (lambda: datetime.now(UTC))
        #resta leggibile anche quando il giro si interrompe a meta'
        self.report = UploadReport()

    def upload_pending(
        self, current_zone: str, country_code: str, skip: set[datetime]
    ) -> UploadReport:
        #skip: ore gia' rifiutate in questa sessione
        report = self.report = UploadReport()
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport) as client:
            status = self._status(client)
            report.study = status.study
            now = self._now()
            end = hour_floor(now - timedelta(seconds=self._settle_seconds))
            ends_at = status.study.ends_at
            study_over = (
                status.phase == StudyPhase.ENDED.value and ends_at is not None and end >= ends_at
            )
            #dopo la fine dello studio non si invia piu' niente
            if ends_at is not None:
                end = min(end, hour_floor(ends_at))
            start = max(
                #il consenso non e' retroattivo: l'ora dell'adesione resta fuori
                hour_ceil(status.joined_at),
                end - timedelta(days=status.max_past_days) + timedelta(hours=1),
            )
            if status.last_hour_start is not None:
                start = max(start, status.last_hour_start + timedelta(hours=1))
            if start >= end:
                report.finished = study_over
                return report

            hours = [
                hour
                for hour in aggregate_hours(
                    self._db.energy_samples_between(start, end),
                    self._db.grid_zones_between(start, end),
                    current_zone,
                )
                if hour.hour_start not in skip
            ]
            for run in zone_runs(hours):
                zone = run[0].zone
                country = country_code if zone == current_zone else zone.split("-", 1)[0]
                self._send_run(client, status, zone, country, run, report)
            report.finished = study_over
        return report

    def _status(self, client: httpx.Client) -> ServerStatus:
        try:
            response = client.get(self._endpoint + STATUS_PATH, headers=self._status_headers)
        except httpx.HTTPError as exc:
            raise UploadStopped(f"server non raggiungibile ({exc})", retry=True) from exc
        if response.status_code == 401:
            raise UploadStopped(
                "token rifiutato: revocato o sbagliato, controllarlo nelle impostazioni",
                retry=False,
                token_rejected=True,
            )
        if response.status_code != 200:
            raise UploadStopped(f"stato non disponibile ({response.status_code})", retry=True)
        try:
            return parse_status(response.json())
        except UploadStopped:
            raise
        except (TypeError, ValueError) as exc:
            raise UploadStopped(f"risposta di stato illeggibile ({exc})", retry=False) from exc

    def _body(self, zone: str, country_code: str, hours: list[HourlyUsage]) -> bytes:
        body = {
            "schema_version": SCHEMA_VERSION,
            "app_version": ecomonitor.__version__,
            "install_id": self._install_id,
            "os_family": self._os_family,
            "device_class": self._device_class,
            "country_code": country_code,
            "zone": zone,
            "hours": [hour.to_payload() for hour in hours],
        }
        #allow_nan=False: un NaN arriverebbe come token non JSON
        return json.dumps(body, allow_nan=False).encode("utf-8")

    def _send_run(
        self,
        client: httpx.Client,
        status: ServerStatus,
        zone: str,
        country_code: str,
        run: list[HourlyUsage],
        report: UploadReport,
    ) -> None:
        pending = list(run)
        size = status.max_hours_per_request
        while pending:
            ends_at = None if report.study is None else report.study.ends_at
            if ends_at is not None:
                pending = [hour for hour in pending if hour.hour_start < ends_at]
                if not pending:
                    break
            chunk = pending[:size]
            raw = self._body(zone, country_code, chunk)
            if len(raw) > status.max_body_bytes:
                if size == 1:
                    raise UploadStopped("un'ora sola supera il limite di byte", retry=False)
                size = max(1, size // 2)
                continue
            try:
                response = client.post(
                    self._endpoint + HOURS_PATH,
                    content=raw,
                    headers={**self._headers, "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise UploadStopped(f"server non raggiungibile ({exc})", retry=True) from exc

            if response.status_code == 200:
                report.accepted += len(chunk)
                del pending[: len(chunk)]
                self._read_study(response, report)
                continue
            reason, index = _rejection(response)
            if response.status_code == 400 and index is not None and 0 <= index < len(chunk):
                dropped = pending.pop(index)
                report.rejected.append(dropped.hour_start)
                logger.warning(
                    "Il server dello studio ha rifiutato l'ora %s UTC (%s): non verra' reinviata",
                    dropped.hour_start.strftime("%Y-%m-%d %H:%M"),
                    reason,
                )
                continue
            if response.status_code == 413 and size > 1:
                size = max(1, size // 2)
                continue
            if response.status_code == 401:
                raise UploadStopped(
                    "token rifiutato: revocato o sbagliato, controllarlo nelle impostazioni",
                    retry=False,
                    token_rejected=True,
                )
            raise UploadStopped(
                f"invio non riuscito ({response.status_code}, {reason})",
                retry=response.status_code >= 500,
            )

    @staticmethod
    def _read_study(response: httpx.Response, report: UploadReport) -> None:
        try:
            _phase, report.study = parse_study(response.json().get("study"))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("Studio: calendario nella risposta all'invio illeggibile (%s)", exc)


@dataclass(frozen=True)
class UploadSettings:
    #copia della configurazione presa sul thread di polling
    endpoint: str
    token: str
    install_id: str
    zone: str
    country_code: str
    settle_seconds: float


class StudyUploadScheduler:
    #Decide quando inviare e fa girare l'invio in un thread suo.

    def __init__(
        self,
        db: LocalDatabase,
        uploader_factory: Callable[..., StudyUploader] = StudyUploader,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._uploader_factory = uploader_factory
        self._clock = clock
        self._next_attempt_at: float | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #ore rifiutate
        self._rejected: set[datetime] = set()
        self._os_family = detect_os_family()
        self._device_class: str | None = None
        self._token_rejected = False
        self._study: StudyDates | None = None
        #studio finito e ogni ora inviata: niente piu' giri fino al riavvio
        self._finished = False
        #cresce a ogni reset
        self._generation = 0

    @property
    def token_rejected(self) -> bool:
        with self._lock:
            return self._token_rejected

    @property
    def study_dates(self) -> StudyDates | None:
        with self._lock:
            return self._study

    def reset(self) -> None:
        #token cambiato
        with self._lock:
            self._next_attempt_at = None
            self._rejected.clear()
            self._token_rejected = False
            self._study = None
            self._finished = False
            self._generation += 1

    def maybe_start(self, settings: UploadSettings | None) -> threading.Thread | None:
        if settings is None or self._os_family is None:
            return None
        with self._lock:
            now = self._clock()
            if self._finished:
                return None
            if self._thread is not None and self._thread.is_alive():
                return None
            if self._next_attempt_at is not None and now < self._next_attempt_at:
                return None
            self._next_attempt_at = now + UPLOAD_INTERVAL_SECONDS
            thread = threading.Thread(
                target=self._run, args=(settings,), name="study-upload", daemon=True
            )
            self._thread = thread
        thread.start()
        return thread

    def _run(self, settings: UploadSettings) -> None:
        uploader: StudyUploader | None = None
        with self._lock:
            generation = self._generation
        try:
            if self._device_class is None:
                self._device_class = detect_device_class()
            with self._lock:
                skip = set(self._rejected)
            uploader = self._uploader_factory(
                self._db,
                settings.endpoint,
                settings.token,
                install_id=settings.install_id,
                os_family=self._os_family,
                device_class=self._device_class,
                settle_seconds=settings.settle_seconds,
            )
            uploader.upload_pending(settings.zone, settings.country_code, skip)
            with self._lock:
                self._token_rejected = False
                if uploader.report.finished and generation == self._generation:
                    self._finished = True
                    logger.info("Studio: finito, ogni ora e' stata inviata")
        except UploadStopped as stop:
            logger.warning("Studio: invio interrotto, %s", stop)
            with self._lock:
                if stop.token_rejected:
                    self._token_rejected = True
                if stop.retry:
                    self._next_attempt_at = self._clock() + RETRY_INTERVAL_SECONDS
        except Exception:
            #un difetto qui non deve toccare la misura
            logger.exception("Studio: invio non riuscito")
        finally:
            if uploader is not None:
                with self._lock:
                    self._rejected.update(uploader.report.rejected)
                    if uploader.report.study is not None and generation == self._generation:
                        self._study = uploader.report.study
                if uploader.report.accepted:
                    logger.info("Studio: %d ore inviate", uploader.report.accepted)
