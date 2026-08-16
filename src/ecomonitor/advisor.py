from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from ecomonitor.carbon.grid_intensity import IntensityPoint

#Suggerisce se conviene ricaricare adesso o aspettare

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


class ReferenceSource(Enum):
    FORECAST_MEAN = "forecast_mean"
    # media dei punti previsti nelle prossime 24 h

    OBSERVED = "observed"
    # media mobile dei campioni osservati, usata come ripiego


_REFERENCE_LABELS = {
    ReferenceSource.FORECAST_MEAN: "la media prevista",
    ReferenceSource.OBSERVED: "la media recente",
}


def _percent_phrase(value: float) -> str:
    #accenti
    n = round(value)
    article = "l'" if n in (1, 8, 11, 18) or 80 <= n <= 89 else "il "
    return f"{article}{n}%"


@dataclass(frozen=True)
class ChargeAdvice:
    recommendation: ChargeRecommendation
    source: AdviceSource
    current_intensity: float | None = None
    best_intensity: float | None = None
    best_time: datetime | None = None
    reference_intensity: float | None = None
    reference_source: ReferenceSource | None = None
    saving_percent: float | None = None

    reference_horizon_hours: float | None = None
    #Su quante ore e' calcolato il riferimento

    def _reference_label(self) -> str:
        label = _REFERENCE_LABELS[self.reference_source]
        if self.reference_source is ReferenceSource.FORECAST_MEAN and self.reference_horizon_hours:
            return f"{label} per le prossime {self.reference_horizon_hours:g} h"
        return label

    def _reference_clause(self) -> str:
        #dice quanto vale il momento in assoluto, che la sola previsione non
        #copre: il minimo delle prossime ore puo' essere comunque un pessimo
        #valore. Il testo cambia con la provenienza del riferimento, che non
        #vale la stessa cosa nei due casi
        if (
            self.current_intensity is None
            or self.reference_intensity is None
            or self.reference_intensity <= 0
            or self.reference_source is None
        ):
            return ""
        label = self._reference_label()
        delta = (self.current_intensity - self.reference_intensity) / self.reference_intensity
        if abs(delta) < 0.01:
            return f", in linea con {label}"
        direction = "sopra" if delta > 0 else "sotto"
        return f", {_percent_phrase(abs(delta) * 100)} {direction} {label}"

    def message(self) -> str:
        #testo pronto per l'interfaccia, una riga
        if self.source is AdviceSource.NO_BATTERY:
            return "non applicabile (dispositivo senza batteria)"
        if self.source is AdviceSource.UNAVAILABLE:
            return "dati di rete non ancora sufficienti"
        if self.source is AdviceSource.LOW_BATTERY:
            return "carica bassa: collega ora, indipendentemente dal mix elettrico"

        current = f"{self.current_intensity:.0f}" if self.current_intensity is not None else "—"
        if self.recommendation is ChargeRecommendation.WAIT:
            if self.best_time is not None:
                local_time = self.best_time.astimezone().strftime("%H:%M")
                return (
                    f"conviene aspettare le {local_time}: "
                    f"{self.best_intensity:.0f} invece di {current} gCO2/kWh "
                    f"(-{self.saving_percent:.0f}%)"
                )
            #previsione su quando andrà meglio
            return f"conviene aspettare, se puoi ({current} gCO2/kWh{self._reference_clause()})"
        if self.recommendation is ChargeRecommendation.CHARGE_NOW:
            return f"buon momento per caricare ({current} gCO2/kWh{self._reference_clause()})"
        if self.source is AdviceSource.FORECAST and self.best_time is not None:
            #NEUTRAL da previsione non vuol dire "nella media": vuol dire che
            #qualcosa di meglio c'e', ma non abbastanza da aspettare
            local_time = self.best_time.astimezone().strftime("%H:%M")
            return (
                f"{current} gCO2/kWh{self._reference_clause()}: aspettare le {local_time} "
                f"farebbe risparmiare solo {_percent_phrase(self.saving_percent or 0.0)}"
            )
        return f"momento nella media ({current} gCO2/kWh{self._reference_clause()})"


class CarbonAwareAdvisor:
    def __init__(
        self,
        good_threshold_ratio: float = 0.85,
        bad_threshold_ratio: float = 1.15,
        min_saving_ratio: float = 0.10,
        at_minimum_ratio: float = 0.02,
        horizon_hours: float = 12.0,
        critical_battery_percent: float = 20.0,
    ) -> None:
        #le due soglie sono rapporti rispetto al riferimento, usate solo nel
        #ripiego su media mobile.
        #at_minimum_ratio: sotto questo scarto dal minimo previsto adesso vale
        #quanto il momento migliore in vista, quindi e' gia' il momento giusto
        self.good_threshold_ratio = good_threshold_ratio
        self.bad_threshold_ratio = bad_threshold_ratio
        self.min_saving_ratio = min_saving_ratio
        self.at_minimum_ratio = at_minimum_ratio
        self.critical_battery_percent = critical_battery_percent

        #una finestra sola per la decisione e per il confronto
        
        self.horizon_hours = horizon_hours

    @property
    def reference_horizon_hours(self) -> float:
        return self.horizon_hours

    def _resolve_reference(
        self,
        forecast: list[IntensityPoint] | None,
        observed: float | None,
        now: datetime,
    ) -> tuple[float | None, ReferenceSource | None]:
        #La media osservata e' distorta dal pattern di utilizzo: l'app campiona
        #solo mentre e' accesa, quindi su un uso diurno mancano proprio le fasce
        #piu' sporche, e "sotto la media" finisce per dire "e' mezzogiorno".
        #La media prevista copre tutte le ore per costruzione, e va preferita
        window_end = now + timedelta(hours=self.reference_horizon_hours)
        upcoming = [
            point.g_per_kwh
            for point in (forecast or [])
            if now < point.timestamp <= window_end and point.g_per_kwh > 0
        ]
        if upcoming:
            return sum(upcoming) / len(upcoming), ReferenceSource.FORECAST_MEAN
        if observed is not None and observed > 0:
            return observed, ReferenceSource.OBSERVED
        return None, None

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

        reference, reference_source = self._resolve_reference(forecast, reference_intensity, now)

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
                    reference_intensity=reference,
                    reference_source=reference_source,
                    reference_horizon_hours=self.reference_horizon_hours,
                    saving_percent=saving * 100.0,
                )
            #niente da aspettare. Se poi lo scarto dal minimo previsto e' dentro
            #at_minimum_ratio, adesso e' di fatto il minimo: decidere fra
            #"carica ora" e "nella media" su due gCO2/kWh contraddirebbe la
            #soglia sopra, che ha appena dichiarato quella differenza irrilevante
            recommendation = (
                ChargeRecommendation.CHARGE_NOW
                if saving <= self.at_minimum_ratio
                else ChargeRecommendation.NEUTRAL
            )
            return ChargeAdvice(
                recommendation=recommendation,
                source=AdviceSource.FORECAST,
                current_intensity=current_intensity,
                best_intensity=best.g_per_kwh,
                best_time=best.timestamp,
                reference_intensity=reference,
                reference_source=reference_source,
                reference_horizon_hours=self.reference_horizon_hours,
                saving_percent=max(0.0, saving * 100.0),
            )

        if reference is not None:
            return ChargeAdvice(
                recommendation=self.recommend(current_intensity, reference),
                source=AdviceSource.HISTORY,
                current_intensity=current_intensity,
                reference_intensity=reference,
                reference_source=reference_source,
                reference_horizon_hours=self.reference_horizon_hours,
            )

        return ChargeAdvice(
            recommendation=ChargeRecommendation.NEUTRAL,
            source=AdviceSource.UNAVAILABLE,
            current_intensity=current_intensity,
        )


class RollingAverage:
    #Media mobile usata come riferimento quando manca la previsione

    def __init__(self, window_size: int = 288, min_samples: int = 12) -> None:
        self.window_size = window_size  # 288 campioni = 24 h a uno ogni 5 minuti
        self.min_samples = min_samples  # 12 campioni = un'ora di campionamento
        self._samples: list[float] = []

    def add(self, value: float) -> None:
        self._samples.append(value)
        if len(self._samples) > self.window_size:
            self._samples.pop(0)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @property
    def average(self) -> float | None:
        #None finche' i campioni non bastano: su un'installazione nuova la
        #media sarebbe il valore corrente confrontato con se stesso, cioe' un
        #riferimento che sembra informativo e non lo e'
        if len(self._samples) < self.min_samples:
            return None
        return sum(self._samples) / len(self._samples)
