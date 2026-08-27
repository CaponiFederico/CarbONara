import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.electricitymaps.com/v3"

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


def _parse_point(item: dict) -> IntensityPoint:
    return IntensityPoint(
        timestamp=datetime.fromisoformat(item["datetime"]),
        g_per_kwh=float(item["carbonIntensity"]),
    )


class LocalIntensityHistory(Protocol):
    #accumula i campioni di tutte le zone in cui il dispositivo si e' trovato

    def mean_grid_intensity_between(
        self, start: datetime, end: datetime, zone: str | None = None
    ) -> float | None: ...


class StaticFallbackProvider:
    def get_current_intensity(self, zone: str) -> float:
        country = country_code_from_zone(zone)
        return STATIC_FALLBACK_G_PER_KWH.get(country, STATIC_FALLBACK_G_PER_KWH["DEFAULT"])


class ElectricityMapsProvider:
    #Client per l'API ElectricityMaps v3

    #Endpoint usati:
    #/carbon-intensity/latest  
    #/carbon-intensity/forecast
    #/carbon-intensity/past-range 

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 8.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        response = httpx.get(
            f"{self._base_url}{path}",
            params=params,
            headers={"auth-token": self._api_key},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_current_intensity(self, zone: str) -> float:
        return float(self._get("/carbon-intensity/latest", {"zone": zone})["carbonIntensity"])

    def get_forecast(self, zone: str) -> list[IntensityPoint]:
        payload = self._get("/carbon-intensity/forecast", {"zone": zone})
        return [_parse_point(item) for item in payload.get("forecast", [])]

    def get_past_range(self, zone: str, start: datetime, end: datetime) -> list[IntensityPoint]:
        payload = self._get(
            "/carbon-intensity/past-range",
            {
                "zone": zone,
                "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            },
        )
        return [_parse_point(item) for item in payload.get("data", [])]


class GridCarbonIntensityService:
    def __init__(
        self,
        zone: str,
        api_key: str | None = None,
        country_code: str | None = None,
        cache_ttl_seconds: float = 300.0,
        forecast_ttl_seconds: float = 1800.0,
        local_history: LocalIntensityHistory | None = None,
        provider: ElectricityMapsProvider | None = None,
    ) -> None:
        self.zone = zone
        self.country_code = country_code or country_code_from_zone(zone)
        self._live = provider or (ElectricityMapsProvider(api_key) if api_key else None)
        self._fallback = StaticFallbackProvider()
        self._local_history = local_history

        self._cache_ttl = cache_ttl_seconds
        self._forecast_ttl = forecast_ttl_seconds
        self._cached_value: float | None = None
        self._cached_at: float | None = None
        self._cached_forecast: list[IntensityPoint] = []
        self._forecast_cached_at: float | None = None
        self._last_value_source: str = "static_fallback"
        self._last_mean_source: str | None = None

    @property
    def has_live_provider(self) -> bool:
        #chiave API configurata Non significa che l'ultimo valore sia live: 
        # ElectricityMaps puo' essere irraggiungibile o la zona non coperta
        return self._live is not None

    @property
    def last_value_source(self) -> str:
        #live se l'ultimo valore restituito da get_current_intensity() arriva da ElectricityMaps
        #static_fallback se e' la media nazionale di ripiego
        return self._last_value_source

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
                logger.warning("ElectricityMaps non raggiungibile (%s), uso il fallback statico", exc)
        else:
            logger.debug(
                "Nessuna chiave ElectricityMaps configurata: uso la media statica per %s. "
                "Imposta electricitymaps_api_key nella configurazione o la variabile "
                "d'ambiente ECOMONITOR_EM_API_KEY per i dati in tempo reale.",
                self.zone,
            )

        if value is None:
            value = self._fallback.get_current_intensity(self.zone)
            self._last_value_source = "static_fallback"
        else:
            self._last_value_source = "live"

        self._cached_value, self._cached_at = value, now
        return value

    def get_forecast(self) -> list[IntensityPoint]:
        #Previsione oraria per le prossime 24 h. Lista vuota se non
        #disponibile (nessuna chiave, API irraggiungibile, zona non coperta)
        now = time.monotonic()
        if (
            self._forecast_cached_at is not None
            and now - self._forecast_cached_at < self._forecast_ttl
        ):
            return self._cached_forecast

        forecast: list[IntensityPoint] = []
        if self._live is not None:
            try:
                forecast = self._live.get_forecast(self.zone)
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                logger.warning("Forecast ElectricityMaps non disponibile (%s)", exc)

        self._cached_forecast, self._forecast_cached_at = forecast, now
        return forecast

    def get_past_points(self, start: datetime, end: datetime) -> list[IntensityPoint]:
        #I punti orari della finestra
        if self._live is None or end <= start:
            return []
        try:
            return self._live.get_past_range(self.zone, start, end)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Storico ElectricityMaps non disponibile (%s)", exc)
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
                logger.warning("Storico ElectricityMaps non disponibile (%s)", exc)

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
