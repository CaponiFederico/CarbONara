from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from ecomonitor.carbon.grid_intensity import IntensityPoint

#Suggerisce se conviene ricaricare adesso o aspettare: qui si guarda avanti,
#il pool guarda indietro. "L'intensita' e' sopra la media" non e' azionabile,
#con la previsione oraria il consiglio diventa "aspetta le 14:00, -38%" ed e'
#verificabile a posteriori. La media mobile resta come ripiego.
#Sotto una soglia critica di carica il consiglio e' sempre "carica ora": un'app
#che lascia l'utente senza batteria per qualche grammo viene disinstallata


class ChargeRecommendation(Enum):
    CHARGE_NOW = "charge_now"
    NEUTRAL = "neutral"
    WAIT = "wait"


class AdviceSource(Enum):
    FORECAST = "forecast"
    # previsione oraria della zona, con orario e risparmio stimato

    HISTORY = "history"
    # nessuna previsione: confronto con la media mobile locale

    LOW_BATTERY = "low_battery"
    # carica sotto la soglia critica

    NO_BATTERY = "no_battery"
    # desktop: non c'e' nessuna ricarica da spostare nel tempo

    UNAVAILABLE = "unavailable"
    # ne' previsione ne' storico sufficiente


@dataclass(frozen=True)
class ChargeAdvice:
    recommendation: ChargeRecommendation
    source: AdviceSource
    current_intensity: float | None = None
    best_intensity: float | None = None
    best_time: datetime | None = None
    reference_intensity: float | None = None
    saving_percent: float | None = None

    def message(self) -> str:
        #testo pronto per l'interfaccia, una riga
        if self.source is AdviceSource.NO_BATTERY:
            return "non applicabile (dispositivo senza batteria)"
        if self.source is AdviceSource.UNAVAILABLE:
            return "dati di rete non ancora sufficienti"
        if self.source is AdviceSource.LOW_BATTERY:
            return "carica bassa: collega ora, indipendentemente dal mix elettrico"

        current = f"{self.current_intensity:.0f}" if self.current_intensity is not None else "—"
        if self.recommendation is ChargeRecommendation.WAIT and self.best_time is not None:
            local_time = self.best_time.astimezone().strftime("%H:%M")
            return (
                f"conviene aspettare le {local_time}: "
                f"{self.best_intensity:.0f} invece di {current} gCO2/kWh "
                f"(-{self.saving_percent:.0f}%)"
            )
        if self.recommendation is ChargeRecommendation.CHARGE_NOW:
            return f"buon momento per caricare ({current} gCO2/kWh)"
        return f"momento nella media ({current} gCO2/kWh)"


class CarbonAwareAdvisor:
    def __init__(
        self,
        good_threshold_ratio: float = 0.85,
        bad_threshold_ratio: float = 1.15,
        min_saving_ratio: float = 0.10,
        horizon_hours: float = 12.0,
        critical_battery_percent: float = 20.0,
    ) -> None:
        #le due soglie sono rapporti rispetto al riferimento, usate solo nel
        #ripiego su media mobile. Sotto min_saving_ratio non vale la pena
        #chiedere di aspettare: la previsione ha un errore proprio
        self.good_threshold_ratio = good_threshold_ratio
        self.bad_threshold_ratio = bad_threshold_ratio
        self.min_saving_ratio = min_saving_ratio
        self.horizon_hours = horizon_hours
        self.critical_battery_percent = critical_battery_percent

    def recommend(
        self, current_intensity: float, reference_intensity: float
    ) -> ChargeRecommendation:
        if reference_intensity <= 0:
            return ChargeRecommendation.NEUTRAL

        ratio = current_intensity / reference_intensity
        if ratio <= self.good_threshold_ratio:
            return ChargeRecommendation.CHARGE_NOW
        if ratio >= self.bad_threshold_ratio:
            return ChargeRecommendation.WAIT
        return ChargeRecommendation.NEUTRAL

    def advise(
        self,
        current_intensity: float,
        forecast: list[IntensityPoint] | None = None,
        reference_intensity: float | None = None,
        battery_percent: float | None = None,
        has_battery: bool = True,
        now: datetime | None = None,
    ) -> ChargeAdvice:
        now = now or datetime.now(UTC)

        if not has_battery:
            return ChargeAdvice(
                recommendation=ChargeRecommendation.NEUTRAL,
                source=AdviceSource.NO_BATTERY,
                current_intensity=current_intensity,
            )

        if battery_percent is not None and battery_percent <= self.critical_battery_percent:
            return ChargeAdvice(
                recommendation=ChargeRecommendation.CHARGE_NOW,
                source=AdviceSource.LOW_BATTERY,
                current_intensity=current_intensity,
            )

        horizon_end = now + timedelta(hours=self.horizon_hours)
        upcoming = [
            point
            for point in (forecast or [])
            if now < point.timestamp <= horizon_end and point.g_per_kwh > 0
        ]

        if upcoming:
            best = min(upcoming, key=lambda point: point.g_per_kwh)
            saving = (
                (current_intensity - best.g_per_kwh) / current_intensity
                if current_intensity > 0
                else 0.0
            )
            if saving >= self.min_saving_ratio:
                return ChargeAdvice(
                    recommendation=ChargeRecommendation.WAIT,
                    source=AdviceSource.FORECAST,
                    current_intensity=current_intensity,
                    best_intensity=best.g_per_kwh,
                    best_time=best.timestamp,
                    saving_percent=saving * 100.0,
                )
            #nessun momento migliore in vista: se l'attuale e' il minimo della
            #finestra e' proprio il momento giusto
            recommendation = (
                ChargeRecommendation.CHARGE_NOW
                if current_intensity <= best.g_per_kwh
                else ChargeRecommendation.NEUTRAL
            )
            return ChargeAdvice(
                recommendation=recommendation,
                source=AdviceSource.FORECAST,
                current_intensity=current_intensity,
                best_intensity=best.g_per_kwh,
                best_time=best.timestamp,
                saving_percent=max(0.0, saving * 100.0),
            )

        if reference_intensity is not None and reference_intensity > 0:
            return ChargeAdvice(
                recommendation=self.recommend(current_intensity, reference_intensity),
                source=AdviceSource.HISTORY,
                current_intensity=current_intensity,
                reference_intensity=reference_intensity,
            )

        return ChargeAdvice(
            recommendation=ChargeRecommendation.NEUTRAL,
            source=AdviceSource.UNAVAILABLE,
            current_intensity=current_intensity,
        )


class RollingAverage:
    #Media mobile usata come riferimento quando manca la previsione. Va
    #alimentata a ogni campione, collegati o no: costruirla solo mentre si e'
    #attaccati significherebbe misurare proprio gli istanti in cui la domanda
    #"conviene attaccare?" non ha senso

    def __init__(self, window_size: int = 288) -> None:
        self.window_size = window_size  # 288 campioni = 24 h a uno ogni 5 minuti
        self._samples: list[float] = []

    def add(self, value: float) -> None:
        self._samples.append(value)
        if len(self._samples) > self.window_size:
            self._samples.pop(0)

    @property
    def average(self) -> float | None:
        if not self._samples:
            return None
        return sum(self._samples) / len(self._samples)
