from dataclasses import dataclass
from enum import Enum

from ecomonitor.carbon.battery_pool import (
    MIN_SIGNIFICANT_DELTA_PERCENT,
    BatteryCarbonPool,
)


class AttributionSource(Enum):
    GRID_LIVE = "grid_live"
    # collegato: intensita' rete

    BATTERY_POOL = "battery_pool"
    # batteria: intensita' ereditata dalle ricariche

    BATTERY_POOL_PARTIAL = "battery_pool_partial"
    # batteria: con pool parziale, miscela fra storico e ripiego

    BATTERY_POOL_FALLBACK = "battery_pool_fallback" 
    # batteria: senza pool, ripiego all'intensita' di rete

FULL_COVERAGE_RATIO = 0.999
#Sopra questa frazione di copertura il prelievo e' considerato interamente
#tracciato, per fifo linux


def gco2_from_wh(wh: float, intensity_g_per_kwh: float) -> float:
    return (wh / 1000.0) * intensity_g_per_kwh


@dataclass(frozen=True)
class AttributionResult:
    gco2: float
    intensity_g_per_kwh: float
    source: AttributionSource
    charged_percent: float = 0.0
    discharged_percent: float = 0.0
    pool_coverage_ratio: float = 1.0
    #Frazione della scarica di questo intervallo coperta dallo storico di
    #ricarica (1.0 se da rete o senza batteria)
    battery_assisted: bool = False
    #True se dispositivo collegato ma la batteria si e' comunque scaricata

    @property
    def is_estimated_intensity(self) -> bool:
        return self.source in (
            AttributionSource.BATTERY_POOL_FALLBACK,
            AttributionSource.BATTERY_POOL_PARTIAL,
        )


class CarbonAttributionService:
    def __init__(self, pool: BatteryCarbonPool) -> None:
        self.pool = pool

    def attribute(
        self,
        *,
        delta_wh: float, #sensori hw
        is_plugged_in: bool,
        battery_percent: float | None,
        previous_battery_percent: float | None,
        grid_intensity_g_per_kwh: float,

        charge_intensity_g_per_kwh: float | None = None,
        #se passato sostituisce l'intensita' live

    ) -> AttributionResult:
        #Attribuisce l'energia consumata in questo intervallo a rete o
        #batteria e aggiorna lo storico di ricarica
        delta_percent = 0.0
        if battery_percent is not None and previous_battery_percent is not None:
            delta_percent = battery_percent - previous_battery_percent

        charged = max(0.0, delta_percent)
        drained = max(0.0, -delta_percent)

        if charged > MIN_SIGNIFICANT_DELTA_PERCENT:
            intensity_for_charge = (
                charge_intensity_g_per_kwh
                if charge_intensity_g_per_kwh is not None
                else grid_intensity_g_per_kwh
            )
            self.pool.charge(charged, intensity_for_charge)

        if is_plugged_in or battery_percent is None:
            return self._attribute_plugged(
                delta_wh=delta_wh,
                grid_intensity_g_per_kwh=grid_intensity_g_per_kwh,
                charged=charged,
                drained=drained,
            )

        return self._attribute_on_battery(
            delta_wh=delta_wh,
            grid_intensity_g_per_kwh=grid_intensity_g_per_kwh,
            charged=charged,
            drained=drained,
        )

    def _attribute_plugged(
        self,
        *,
        delta_wh: float,
        grid_intensity_g_per_kwh: float,
        charged: float,
        drained: float,
    ) -> AttributionResult:
        #Collegato o desktop: l'energia consumata arriva dal muro

        if drained > MIN_SIGNIFICANT_DELTA_PERCENT:
            self.pool.discharge_with_coverage(drained)

        return AttributionResult(
            gco2=gco2_from_wh(delta_wh, grid_intensity_g_per_kwh),
            intensity_g_per_kwh=grid_intensity_g_per_kwh,
            source=AttributionSource.GRID_LIVE,
            charged_percent=charged,
            discharged_percent=drained,
            battery_assisted=drained > MIN_SIGNIFICANT_DELTA_PERCENT,
        )

    def _attribute_on_battery(
        self,
        *,
        delta_wh: float,
        grid_intensity_g_per_kwh: float,
        charged: float,
        drained: float,
    ) -> AttributionResult:
        #Scollegato: l'intensita' la decide lo storico di ricarica
        had_history = self.pool.has_history
        outcome = self.pool.discharge_with_coverage(drained)
        coverage = outcome.coverage_ratio if had_history else 0.0
        pool_intensity = outcome.intensity_g_per_kwh

        if not had_history or pool_intensity <= 0.0:
            return AttributionResult(
                gco2=gco2_from_wh(delta_wh, grid_intensity_g_per_kwh),
                intensity_g_per_kwh=grid_intensity_g_per_kwh,
                source=AttributionSource.BATTERY_POOL_FALLBACK,
                charged_percent=charged,
                discharged_percent=drained,
                pool_coverage_ratio=0.0,
            )

        if coverage >= FULL_COVERAGE_RATIO:
            return AttributionResult(
                gco2=gco2_from_wh(delta_wh, pool_intensity),
                intensity_g_per_kwh=pool_intensity,
                source=AttributionSource.BATTERY_POOL,
                charged_percent=charged,
                discharged_percent=drained,
                pool_coverage_ratio=coverage,
            )

        # Copertura parziale miscela storico e ripiego 
        blended = coverage * pool_intensity + (1.0 - coverage) * grid_intensity_g_per_kwh
        return AttributionResult(
            gco2=gco2_from_wh(delta_wh, blended),
            intensity_g_per_kwh=blended,
            source=AttributionSource.BATTERY_POOL_PARTIAL,
            charged_percent=charged,
            discharged_percent=drained,
            pool_coverage_ratio=coverage,
        )
