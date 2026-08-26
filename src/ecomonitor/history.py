from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

from ecomonitor.storage.db import EnergySample, LocalDatabase, local_day_bounds

#Aggregati per periodo, letti dal database

SECONDS_IN_HOUR = 3600.0
HOURS_IN_DAY = 24

#Una fascia oraria entra nella media solo se e' stata osservata 
#per almeno questa frazione di un'ora
HOUR_COMPLETE_RATIO = 0.9

#Copertura di una riga a cui manca duration_seconds
FALLBACK_MAX_GAP_SECONDS = 60.0

#Quanto tempo minimo deve essere stato osservato per confrontare la media
#misurata con quella stimata: se una delle due e' stata osservata solo
#per pochi secondi, il rapporto e' rumoroso e non ha senso pubblicarlo
MIN_COMPARABLE_SECONDS = 600.0


class Period(Enum):
    #I periodi di aggregazione dello storico
    TODAY = "today"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"

    @property
    def days(self) -> int:
        return {Period.TODAY: 1, Period.LAST_7_DAYS: 7, Period.LAST_30_DAYS: 30}[self]

    @property
    def label(self) -> str:
        return {
            Period.TODAY: "oggi",
            Period.LAST_7_DAYS: "7 giorni",
            Period.LAST_30_DAYS: "30 giorni",
        }[self]

    @property
    def description(self) -> str:
        if self is Period.TODAY:
            return "dalla mezzanotte di oggi"
        return f"gli ultimi {self.days} giorni, oggi compreso"


def period_bounds(period: Period, today: date | None = None) -> tuple[datetime, datetime]:
    #Estremi locali del period
    today = today or date.today()
    start, _ = local_day_bounds(today - timedelta(days=period.days - 1))
    _, end = local_day_bounds(today)
    return start, end


@dataclass
class _HourBucket:
    #accumulatore di una singola coppia (giorno, ora)
    wh: float = 0.0
    seconds: float = 0.0
    estimated_wh: float = 0.0


@dataclass(frozen=True)
class HourSlot:
    #Una delle 24 fasce del profilo, gia' mediata sui giorni del periodo
    hour: int
    mean_w: float | None
    days_counted: int
    days_partial: int

    #Quanta dell'energia che forma questa barra viene dal modello invece che da
    #un sensore
    estimated_share: float = 0.0

    @property
    def has_value(self) -> bool:
        return self.mean_w is not None

    @property
    def caption(self) -> str:
        if self.mean_w is None:
            if self.days_partial:
                return (
                    f"{self.hour:02d}:00 — nessun giorno con l'ora intera "
                    f"({self.days_partial} osservata solo in parte)"
                )
            return f"{self.hour:02d}:00 — mai osservata"

        days_word = "giorno" if self.days_counted == 1 else "giorni"
        partial_note = (
            f", {self.days_partial} scartato perche' parziale" if self.days_partial else ""
        )
        estimated_note = (
            f", {self.estimated_share * 100:.0f}% stimato"
            if self.estimated_share > 0
            else ""
        )
        return (
            f"{self.hour:02d}:00 — {self.mean_w:.1f} W medi su "
            f"{self.days_counted} {days_word}{partial_note}{estimated_note}"
        )


@dataclass(frozen=True)
class RangeSummary:
    period: Period
    start: datetime
    end: datetime
    #istante in cui questi numeri sono stati letti
    generated_at: datetime
    elapsed_seconds: float

    energy_wh: float
    operational_gco2: float
    observed_seconds: float

    measured_wh: float
    measured_seconds: float
    estimated_wh: float
    estimated_seconds: float

    plugged_wh: float
    battery_wh: float

    grid_mean_intensity: float | None
    source_wh: dict[str, float] = field(default_factory=dict)
    hours: tuple[HourSlot, ...] = ()
    days_with_data: int = 0

    # -- presenza di dati --------------------------------------------------

    @property
    def has_data(self) -> bool:
        return self.observed_seconds > 0.0

    @property
    def observed_hours(self) -> float:
        return self.observed_seconds / SECONDS_IN_HOUR

    @property
    def coverage_ratio(self) -> float:
        #quanta parte del tempo trascorso nel periodo e' stata davvero misurata
        if self.elapsed_seconds <= 0:
            return 0.0
        return min(1.0, self.observed_seconds / self.elapsed_seconds)

    # -- intensita' --------------------------------------------------------

    @property
    def applied_intensity(self) -> float | None:
        #Media pesata sull'energia
        if self.energy_wh <= 0:
            return None
        return self.operational_gco2 / self.energy_wh * 1000.0

    @property
    def intensity_delta(self) -> float | None:
        #Quanto l'intensita' pagata si discosta dalla media
        #della rete nello stesso periodo

        #Negativo = rete piu' pulita del suo solito
        #Positivo = rete piu' sporca del suo solito
        applied = self.applied_intensity
        if applied is None or self.grid_mean_intensity is None:
            return None
        return applied - self.grid_mean_intensity

    # -- misura contro stima -----------------------------------------------

    @property
    def measured_share(self) -> float | None:
        if self.energy_wh <= 0:
            return None
        return self.measured_wh / self.energy_wh

    @property
    def mean_w(self) -> float | None:
        return _mean_power(self.energy_wh, self.observed_seconds)

    @property
    def mean_w_measured(self) -> float | None:
        return _mean_power(self.measured_wh, self.measured_seconds)

    @property
    def mean_w_estimated(self) -> float | None:
        return _mean_power(self.estimated_wh, self.estimated_seconds)

    @property
    def has_both_populations(self) -> bool:
        return self.measured_seconds > 0 and self.estimated_seconds > 0

    @property
    def bias_is_comparable(self) -> bool:
        #devono essere state osservate abbastanza entrambe le popolazioni per confrontarle
        return (
            self.measured_seconds >= MIN_COMPARABLE_SECONDS
            and self.estimated_seconds >= MIN_COMPARABLE_SECONDS
        )

    @property
    def estimated_bias_ratio(self) -> float | None:
        #Di quanto il modello di ripiego si discosta dalla misura reale
        if not self.bias_is_comparable:
            return None
        measured = self.mean_w_measured
        estimated = self.mean_w_estimated
        if measured is None or estimated is None or measured <= 0:
            return None
        return (estimated - measured) / measured

    @property
    def battery_share(self) -> float | None:
        if self.energy_wh <= 0:
            return None
        return self.battery_wh / self.energy_wh


def _mean_power(wh: float, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return wh / (seconds / SECONDS_IN_HOUR)


def _covered_seconds(samples: list[EnergySample]) -> list[float]:
    #Secondi che ogni riga copre
    covered: list[float] = []
    previous: datetime | None = None
    for sample in samples:
        own = sample.duration_seconds
        if own is None or own <= 0:
            elapsed = (sample.timestamp - previous).total_seconds() if previous else 0.0
            own = elapsed if 0 < elapsed <= FALLBACK_MAX_GAP_SECONDS else 0.0
        covered.append(min(own, SECONDS_IN_HOUR))
        previous = sample.timestamp
    return covered


def _hour_slots(buckets: dict[tuple[date, int], _HourBucket]) -> tuple[HourSlot, ...]:
    threshold = HOUR_COMPLETE_RATIO * SECONDS_IN_HOUR

    slots: list[HourSlot] = []
    for hour in range(HOURS_IN_DAY):
        complete: list[_HourBucket] = []
        partial = 0
        for (_, bucket_hour), bucket in buckets.items():
            if bucket_hour != hour or bucket.seconds <= 0:
                continue
            if bucket.seconds >= threshold:
                complete.append(bucket)
            else:
                partial += 1

        #media dei giorni e non dei watt
        mean_w = None
        if complete:
            per_day = [bucket.wh / (bucket.seconds / SECONDS_IN_HOUR) for bucket in complete]
            mean_w = sum(per_day) / len(per_day)

        #quota pesata sull'energia dei soli giorni che formano la barra
        totale = sum(bucket.wh for bucket in complete)
        stimata = sum(bucket.estimated_wh for bucket in complete)

        slots.append(
            HourSlot(
                hour=hour,
                mean_w=mean_w,
                days_counted=len(complete),
                days_partial=partial,
                estimated_share=(stimata / totale) if totale > 0 else 0.0,
            )
        )
    return tuple(slots)


def build_range_summary(
    db: LocalDatabase, period: Period, now: datetime | None = None
) -> RangeSummary:
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        #gli estremi del periodo sono consapevoli del fuso
        now = now.astimezone()
    start, end = period_bounds(period, now.date())
    samples = db.energy_samples_between(start, end)
    covered = _covered_seconds(samples)

    buckets: dict[tuple[date, int], _HourBucket] = {}
    source_wh: dict[str, float] = {}
    energy_wh = operational_gco2 = observed_seconds = 0.0
    measured_wh = measured_seconds = estimated_wh = estimated_seconds = 0.0
    plugged_wh = battery_wh = 0.0

    for sample, seconds in zip(samples, covered, strict=True):
        #Ora locale della riga
        local = sample.timestamp.astimezone()
        bucket = buckets.setdefault((local.date(), local.hour), _HourBucket())
        bucket.wh += sample.wh
        bucket.seconds += seconds
        if sample.is_estimated:
            bucket.estimated_wh += sample.wh

        energy_wh += sample.wh
        operational_gco2 += sample.gco2
        observed_seconds += seconds

        if sample.is_estimated:
            estimated_wh += sample.wh
            estimated_seconds += seconds
        else:
            measured_wh += sample.wh
            measured_seconds += seconds

        if sample.is_plugged:
            plugged_wh += sample.wh
        else:
            battery_wh += sample.wh

        if sample.attribution_source:
            source_wh[sample.attribution_source] = (
                source_wh.get(sample.attribution_source, 0.0) + sample.wh
            )

    return RangeSummary(
        period=period,
        start=start,
        end=end,
        generated_at=now,
        elapsed_seconds=max(0.0, (min(now, end) - start).total_seconds()),
        energy_wh=energy_wh,
        operational_gco2=operational_gco2,
        observed_seconds=observed_seconds,
        measured_wh=measured_wh,
        measured_seconds=measured_seconds,
        estimated_wh=estimated_wh,
        estimated_seconds=estimated_seconds,
        plugged_wh=plugged_wh,
        battery_wh=battery_wh,
        grid_mean_intensity=db.mean_grid_intensity_between(start, end),
        source_wh=source_wh,
        hours=_hour_slots(buckets),
        days_with_data=len({day for day, _ in buckets}),
    )
