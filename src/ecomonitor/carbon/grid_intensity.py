import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

#I dati di ElectricityMaps arrivano dal server di EcoMonitor 
GRID_LATEST_PATH = "/v1/grid/latest"
GRID_FORECAST_PATH = "/v1/grid/forecast"
GRID_PAST_PATH = "/v1/grid/past"

CONNECT_TIMEOUT_SECONDS = 2.0

# Fallback offline (gCO2eq/kWh, media annuale lifecycle indicativa per paese)
STATIC_FALLBACK_G_PER_KWH: dict[str, float] = {
    "IT": 320.0,
    "FR": 60.0,
    "DE": 380.0,
    "ES": 200.0,
    "PL": 650.0,
    "SE": 40.0,
    "NO": 20.0,
    "GB": 250.0,
    "NL": 350.0,
    "DEFAULT": 475.0,  # media globale, stesso fallback usato da CodeCarbon
}


@dataclass(frozen=True)
class IntensityPoint:
    timestamp: datetime
    g_per_kwh: float


def country_code_from_zone(zone: str) -> str:
    #IT-CNO -> IT. Le zone ElectricityMaps sono ISO paese-sottozona
    return zone.split("-", 1)[0].upper()


def _intensity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"intensita' non numerica: {value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"intensita' fuori dai limiti: {value!r}")
    return number


def _parse_point(item: dict) -> IntensityPoint:
    moment = datetime.fromisoformat(item["datetime"])
    if moment.tzinfo is None:
        raise ValueError(f"ora senza fuso: {item['datetime']!r}")
    return IntensityPoint(timestamp=moment, g_per_kwh=_intensity(item["g_per_kwh"]))


#Ripiego per fascia oraria
HOUR_FALLBACK_DAYS = 7

MIN_HOUR_FALLBACK_SAMPLES = 12

MIN_HOUR_FALLBACK_DAYS = 2

#mediana e non media
HOUR_MEDIAN_SOURCE = "local_hour_median"

LIVE_FAILURE_ZONE = "zone"

LIVE_FAILURE_UNAVAILABLE = "unavailable"

LIVE_FAILURE_UNREACHABLE = "unreachable"


def live_failure_reason(exc: Exception) -> str:
    #perche' non c'e' il dato in tempo reale
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 404:
            return LIVE_FAILURE_ZONE
        if exc.response.status_code == 503:
            return LIVE_FAILURE_UNAVAILABLE
    return LIVE_FAILURE_UNREACHABLE


class GridHourSample(Protocol):
    @property
    def timestamp(self) -> datetime: ...

    @property
    def g_per_kwh(self) -> float: ...


class LocalIntensityHistory(Protocol):
    #accumula i campioni di tutte le zone in cui il dispositivo si e' trovato

    def mean_grid_intensity_between(
        self, start: datetime, end: datetime, zone: str | None = None
    ) -> float | None: ...

    def grid_intensity_hour_samples(
        self, hour: int, start: datetime, end: datetime, zone: str | None = None
    ) -> Sequence[GridHourSample]: ...


class StaticFallbackProvider:
    def get_current_intensity(self, zone: str) -> float:
        country = country_code_from_zone(zone)
        return STATIC_FALLBACK_G_PER_KWH.get(country, STATIC_FALLBACK_G_PER_KWH["DEFAULT"])


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GridServerProvider:
    def __init__(
        self,
        endpoint: str,
        timeout: float = 8.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        #per i test di contratto col server vero
        self._transport = transport

    def _get(self, path: str, params: dict[str, str]) -> Any:
        timeout = httpx.Timeout(self._timeout, connect=min(self._timeout, CONNECT_TIMEOUT_SECONDS))
        with httpx.Client(timeout=timeout, transport=self._transport) as client:
            response = client.get(f"{self._endpoint}{path}", params=params)
            response.raise_for_status()
            return response.json()

    def get_current_intensity(self, zone: str) -> float:
        return _intensity(self._get(GRID_LATEST_PATH, {"zone": zone})["g_per_kwh"])

    def get_forecast(self, zone: str) -> list[IntensityPoint]:
        payload = self._get(GRID_FORECAST_PATH, {"zone": zone})
        return [_parse_point(item) for item in payload["forecast"]]

    def get_past_range(self, zone: str, start: datetime, end: datetime) -> list[IntensityPoint]:
        payload = self._get(
            GRID_PAST_PATH, {"zone": zone, "start": _utc_text(start), "end": _utc_text(end)}
        )
        return [_parse_point(item) for item in payload["data"]]


class GridCarbonIntensityService:
    def __init__(
        self,
        zone: str,
        endpoint: str | None = None,
        country_code: str | None = None,
        cache_ttl_seconds: float = 300.0,
        forecast_ttl_seconds: float = 1800.0,
        local_history: LocalIntensityHistory | None = None,
        provider: GridServerProvider | None = None,
    ) -> None:
        self.zone = zone
        self.country_code = country_code or country_code_from_zone(zone)
        #senza indirizzo del server (da sorgente, senza study_endpoint) solo il ripiego
        self._live = provider or (GridServerProvider(endpoint) if endpoint else None)
        self._fallback = StaticFallbackProvider()
        self._local_history = local_history

        self._cache_ttl = cache_ttl_seconds
        self._forecast_ttl = forecast_ttl_seconds
        self._cached_value: float | None = None
        self._cached_at: float | None = None
        self._cached_forecast: list[IntensityPoint] = []
        self._forecast_cached_at: float | None = None
        self._forecast_valid_for = forecast_ttl_seconds
        self._forecast_failing = False
        self._last_value_source: str = "static_fallback"
        self._last_mean_source: str | None = None
        self._last_live_failure: str | None = None

    def set_zone(self, zone: str, country_code: str | None = None) -> bool:
        #Cambia zona a caldo, e paese se passato: il ripiego statico e' per paese
        country_changed = bool(country_code) and country_code != self.country_code
        if zone == self.zone and not country_changed:
            return False

        self.zone = zone
        if country_code:
            self.country_code = country_code
        #le cache sono della zona vecchia
        self._cached_value = None
        self._cached_at = None
        self._cached_forecast = []
        self._forecast_cached_at = None
        self._forecast_failing = False
        self._last_value_source = "static_fallback"
        self._last_live_failure = None
        return True

    @property
    def has_live_provider(self) -> bool:
        return self._live is not None

    @property
    def last_value_source(self) -> str:
        #da dove viene l'ultimo valore di get_current_intensity()
        return self._last_value_source

    @property
    def last_live_failure(self) -> str | None:
        return self._last_live_failure

    def hour_median_intensity(self, now: datetime | None = None) -> float | None:
        #Mediana dei campioni misurati nella fascia oraria
        if self._local_history is None:
            return None

        moment = (now or datetime.now(UTC)).astimezone()
        start = moment - timedelta(days=HOUR_FALLBACK_DAYS)
        try:
            samples = self._local_history.grid_intensity_hour_samples(
                moment.hour, start, moment, self.zone
            )
        except Exception:
            #il database non deve poter far fallire un tick
            logger.exception("Ripiego per fascia oraria non calcolabile")
            return None

        if len(samples) < MIN_HOUR_FALLBACK_SAMPLES:
            logger.debug(
                "Fascia %02d:00: %d campioni misurati, ne servono %d: resto sulla "
                "media nazionale",
                moment.hour,
                len(samples),
                MIN_HOUR_FALLBACK_SAMPLES,
            )
            return None

        days = {sample.timestamp.astimezone().date() for sample in samples}
        if len(days) < MIN_HOUR_FALLBACK_DAYS:
            logger.debug(
                "Fascia %02d:00: campioni da %d giorno solo, ne servono %d: resto "
                "sulla media nazionale",
                moment.hour,
                len(days),
                MIN_HOUR_FALLBACK_DAYS,
            )
            return None

        return median(sample.g_per_kwh for sample in samples)

    @property
    def static_country_mean(self) -> float:
        #Media annuale del paese, usata come riferimento per l'energia di cui
        #non si conosce l'istante di ricarica
        return STATIC_FALLBACK_G_PER_KWH.get(
            self.country_code, STATIC_FALLBACK_G_PER_KWH["DEFAULT"]
        )

    def get_current_intensity(self) -> float:
        now = time.monotonic()
        if (
            self._cached_value is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_ttl
        ):
            return self._cached_value

        value: float | None = None
        if self._live is not None:
            try:
                value = self._live.get_current_intensity(self.zone)
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                previous = self._last_live_failure
                self._last_live_failure = live_failure_reason(exc)
                logger.log(
                    logging.DEBUG if previous == self._last_live_failure else logging.WARNING,
                    "Dati di rete in tempo reale non disponibili per %s (%s), uso il ripiego",
                    self.zone,
                    exc,
                )
        else:
            logger.debug(
                "Nessun server EcoMonitor configurato: uso la media statica per %s",
                self.zone,
            )

        if value is not None:
            self._last_value_source = "live"
            self._last_live_failure = None
        else:
            #prima della media nazionale
            value = self.hour_median_intensity()
            if value is not None:
                self._last_value_source = HOUR_MEDIAN_SOURCE
            else:
                value = self._fallback.get_current_intensity(self.zone)
                self._last_value_source = "static_fallback"

        self._cached_value, self._cached_at = value, now
        return value

    def get_forecast(self) -> list[IntensityPoint]:
        #Previsione oraria per le prossime 24 h
        now = time.monotonic()
        if (
            self._forecast_cached_at is not None
            and now - self._forecast_cached_at < self._forecast_valid_for
        ):
            return self._cached_forecast

        forecast: list[IntensityPoint] = []
        failed = False
        if self._live is not None:
            try:
                forecast = self._live.get_forecast(self.zone)
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                failed = True
                #una volta per episodio
                logger.log(
                    logging.DEBUG if self._forecast_failing else logging.WARNING,
                    "Previsione di rete non disponibile (%s)",
                    exc,
                )

        self._forecast_failing = failed
        self._forecast_valid_for = (
            min(self._cache_ttl, self._forecast_ttl) if failed else self._forecast_ttl
        )
        self._cached_forecast, self._forecast_cached_at = forecast, now
        return forecast

    def get_past_points(self, start: datetime, end: datetime) -> list[IntensityPoint]:
        #I punti orari della finestra
        if self._live is None or end <= start:
            return []
        try:
            return self._live.get_past_range(self.zone, start, end)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Storico di rete non disponibile (%s)", exc)
            return []

    def get_mean_intensity_between(self, start: datetime, end: datetime) -> float | None:
        #Media dell'intensita' nella finestra [start, end], per ricostruire
        #l'intensita' di una ricarica avvenuta ad app chiusa

        self._last_mean_source = None
        if end <= start:
            return None

        if self._live is not None:
            try:
                points = self._live.get_past_range(self.zone, start, end)
                if points:
                    self._last_mean_source = "reconstructed_api"
                    return sum(point.g_per_kwh for point in points) / len(points)
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                logger.warning("Storico di rete non disponibile (%s)", exc)

        if self._local_history is not None:
            local_mean = self._local_history.mean_grid_intensity_between(start, end, self.zone)
            if local_mean is not None:
                self._last_mean_source = "reconstructed_local"
            return local_mean

        return None

    @property
    def last_mean_source(self) -> str | None:
        #Da dove viene l'ultima media restituita da get_mean_intensity_between():
        #reconstructed_api reconstructed_local o None se non e' stato possibile ricostruirla
        return self._last_mean_source
