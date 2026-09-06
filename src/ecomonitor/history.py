from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

from ecomonitor.carbon.attribution import AttributionSource
from ecomonitor.storage.db import (
    LIVE_SAMPLE_SOURCE,
    MEASURED_SOURCES,
    EnergySample,
    GridSample,
    LocalDatabase,
    grid_window_from_samples,
    local_day_bounds,
)

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

GRID_SAMPLE_MAX_GAP_SECONDS = 900.0

#Sotto questa copertura la media dei campioni non e' un termine di paragone per
#il periodo
MIN_GRID_COVERAGE_RATIO = 0.5

#Le righe attribuite alla rete
GRID_ATTRIBUTION_SOURCES = (AttributionSource.GRID_LIVE.value,)

#Batteria con storico di ricarica: intensita' vera ma di un altro momento
POOL_ATTRIBUTION_SOURCES = (
    AttributionSource.BATTERY_POOL.value,
    AttributionSource.BATTERY_POOL_PARTIAL.value,
)

#Batteria senza storico: l'intensita' viene da un riferimento esterno e non
#misura nessun istante

#resta fuori da ogni confronto
FALLBACK_ATTRIBUTION_SOURCES = (AttributionSource.BATTERY_POOL_FALLBACK.value,)

# sorgente su cui le emissioni evitate hanno senso
AVOIDED_COMPARABLE_SOURCE = AttributionSource.BATTERY_POOL.value

#Oltre questo il campione non descrive piu' l'istante della riga
AVOIDED_MAX_MATCH_SECONDS = 300.0

#Sotto un'ora di righe collegate il saldo non descrive niente
MIN_AVOIDED_ROWS = 60

#Sotto questa quota di energia da batteria collegata, il saldo descrive un
#ritaglio del consumo a batteria e non il consumo a batteria
MIN_AVOIDED_MATCH_SHARE = 0.5


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
class GridHourSlot:
    #L'intensita' media della rete in una fascia oraria

    hour: int
    mean_g_per_kwh: float | None
    samples: int
    #quanti campioni della fascia sono letture vive e quanti ricostruiti
    observed_samples: int = 0

    @property
    def has_value(self) -> bool:
        return self.mean_g_per_kwh is not None

    @property
    def caption(self) -> str:
        if self.mean_g_per_kwh is None:
            return f"{self.hour:02d}:00 — intensita' della rete mai osservata"

        origin = "osservata" if self.observed_samples else "ricostruita"
        if self.observed_samples and self.observed_samples < self.samples:
            origin = "osservata in parte"
        return (
            f"{self.hour:02d}:00 — {self.mean_g_per_kwh:.0f} gCO2/kWh medi "
            f"su {self.samples} campioni, {origin}"
        )


def _grid_hour_slots(samples: list[GridSample]) -> tuple[GridHourSlot, ...]:
    #Media semplice dei campioni della fascia
    totals: dict[int, list[float]] = {}
    observed: dict[int, int] = {}
    for sample in samples:
        hour = sample.timestamp.astimezone().hour
        totals.setdefault(hour, []).append(sample.g_per_kwh)
        if sample.source == LIVE_SAMPLE_SOURCE:
            observed[hour] = observed.get(hour, 0) + 1

    slots = []
    for hour in range(HOURS_IN_DAY):
        values = totals.get(hour, [])
        slots.append(
            GridHourSlot(
                hour=hour,
                mean_g_per_kwh=(sum(values) / len(values)) if values else None,
                samples=len(values),
                observed_samples=observed.get(hour, 0),
            )
        )
    return tuple(slots)


@dataclass(frozen=True)
class AvoidedEmissions:
    #Emissioni evitate, o in piu'

    saved_gco2: float = 0.0
    lost_gco2: float = 0.0
    #energia da batteria che il confronto ha davvero coperto
    wh: float = 0.0
    rows: int = 0
    #energia da batteria senza un campione di rete utilizzabile vicino
    unmatched_wh: float = 0.0
    unmatched_rows: int = 0
    #energia da batteria con pool parziale o assente
    partial_wh: float = 0.0

    @property
    def balance_gco2(self) -> float:
        #Positivo evitato, negativo emesso
        return self.saved_gco2 - self.lost_gco2

    @property
    def battery_wh(self) -> float:
        return self.wh + self.unmatched_wh + self.partial_wh

    @property
    def has_battery(self) -> bool:
        #Su una macchina sempre collegata questo numero non esiste
        return self.battery_wh > 0

    @property
    def matched_share(self) -> float:
        total = self.wh + self.unmatched_wh
        return self.wh / total if total > 0 else 0.0

    @property
    def is_publishable(self) -> bool:
        return self.rows >= MIN_AVOIDED_ROWS and self.matched_share >= MIN_AVOIDED_MATCH_SHARE


def compute_avoided(
    samples: list[EnergySample],
    grid_samples: list[GridSample],
    max_match_seconds: float = AVOIDED_MAX_MATCH_SECONDS,
) -> AvoidedEmissions:
    #Collega ogni riga al campione di rete piu' vicino e somma le differenze
    saved = lost = matched_wh = unmatched_wh = partial_wh = 0.0
    rows = unmatched_rows = 0
    index = 0

    for sample in samples:
        if sample.wh <= 0:
            continue
        if sample.attribution_source in POOL_ATTRIBUTION_SOURCES + FALLBACK_ATTRIBUTION_SOURCES:
            if sample.attribution_source != AVOIDED_COMPARABLE_SOURCE:
                partial_wh += sample.wh
                continue
        else:
            continue

        while index + 1 < len(grid_samples) and abs(
            (grid_samples[index + 1].timestamp - sample.timestamp).total_seconds()
        ) <= abs((grid_samples[index].timestamp - sample.timestamp).total_seconds()):
            index += 1

        nearest = grid_samples[index] if grid_samples else None
        if (
            nearest is None
            or abs((nearest.timestamp - sample.timestamp).total_seconds()) > max_match_seconds
            or nearest.source != LIVE_SAMPLE_SOURCE
        ):
            unmatched_wh += sample.wh
            unmatched_rows += 1
            continue

        #gco2 == wh / 1000 * intensita', identita' che il buffer mantiene esatta
        paid = sample.gco2 / sample.wh * 1000.0
        avoided = sample.wh / 1000.0 * (nearest.g_per_kwh - paid)
        if avoided >= 0:
            saved += avoided
        else:
            lost -= avoided
        matched_wh += sample.wh
        rows += 1

    return AvoidedEmissions(
        saved_gco2=saved,
        lost_gco2=lost,
        wh=matched_wh,
        rows=rows,
        unmatched_wh=unmatched_wh,
        unmatched_rows=unmatched_rows,
        partial_wh=partial_wh,
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
    #quanta parte del periodo i campioni di rete coprono davvero
    grid_coverage_ratio: float = 0.0
    grid_covered_seconds: float = 0.0
    #la copertura divisa per provenienza
    grid_observed_seconds: float = 0.0
    grid_reconstructed_seconds: float = 0.0
    grid_samples: int = 0
    source_wh: dict[str, float] = field(default_factory=dict)
    source_gco2: dict[str, float] = field(default_factory=dict)
    avoided: AvoidedEmissions = field(default_factory=AvoidedEmissions)
    hours: tuple[HourSlot, ...] = ()
    grid_hours: tuple[GridHourSlot, ...] = ()
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

    def _wh_from(self, sources: tuple[str, ...]) -> float:
        return sum(self.source_wh.get(source, 0.0) for source in sources)

    def _intensity_from(self, sources: tuple[str, ...]) -> float | None:
        wh = self._wh_from(sources)
        if wh <= 0:
            return None
        return sum(self.source_gco2.get(source, 0.0) for source in sources) / wh * 1000.0

    @property
    def grid_wh(self) -> float:
        return self._wh_from(GRID_ATTRIBUTION_SOURCES)

    @property
    def pool_wh(self) -> float:
        return self._wh_from(POOL_ATTRIBUTION_SOURCES)

    @property
    def fallback_wh(self) -> float:
        return self._wh_from(FALLBACK_ATTRIBUTION_SOURCES)

    @property
    def applied_grid_intensity(self) -> float | None:
        #Intensita' pagata sulla sola energia presa dalla rete nel periodo
        return self._intensity_from(GRID_ATTRIBUTION_SOURCES)

    @property
    def applied_pool_intensity(self) -> float | None:
        #Intensita' ereditata dalle ricariche
        return self._intensity_from(POOL_ATTRIBUTION_SOURCES)

    @property
    def has_enough_grid_coverage(self) -> bool:
        return (
            self.grid_mean_intensity is not None
            and self.grid_coverage_ratio >= MIN_GRID_COVERAGE_RATIO
        )

    @property
    def intensity_delta(self) -> float | None:
        #Quanto l'intensita' pagata sull'energia di rete si discosta dalla media
        #della rete nelle stesse ore
        applied = self.applied_grid_intensity
        mean = self.grid_mean_intensity
        if applied is None or mean is None or not self.has_enough_grid_coverage:
            return None
        return applied - mean

    @property
    def load_shift_gco2(self) -> float | None:
        #Lo stesso confronto in grammi
        delta = self.intensity_delta
        if delta is None:
            return None
        return -delta * self.grid_wh / 1000.0

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
        total_wh = sum(bucket.wh for bucket in complete)
        estimated_wh = sum(bucket.estimated_wh for bucket in complete)

        slots.append(
            HourSlot(
                hour=hour,
                mean_w=mean_w,
                days_counted=len(complete),
                days_partial=partial,
                estimated_share=(estimated_wh / total_wh) if total_wh > 0 else 0.0,
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
    source_gco2: dict[str, float] = {}
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
            source_gco2[sample.attribution_source] = (
                source_gco2.get(sample.attribution_source, 0.0) + sample.gco2
            )

    #solo le fonti che sono davvero dati di rete
    grid_samples = db.grid_intensity_samples_between(start, end, sources=MEASURED_SOURCES)

    #la copertura si misura sul tempo gia' trascorso: le ore che il periodo
    #non ha ancora raggiunto non sono ore mancanti
    horizon = min(now, end)
    grid = grid_window_from_samples(
        [sample for sample in grid_samples if sample.timestamp < horizon],
        horizon,
        GRID_SAMPLE_MAX_GAP_SECONDS,
        start=start,
    )

    battery_sources = POOL_ATTRIBUTION_SOURCES + FALLBACK_ATTRIBUTION_SOURCES
    if any(source in source_wh for source in battery_sources):
        avoided = compute_avoided(samples, grid_samples)
    else:
        avoided = AvoidedEmissions()

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
        grid_mean_intensity=grid.mean_g_per_kwh,
        grid_coverage_ratio=grid.coverage_ratio,
        grid_covered_seconds=grid.covered_seconds,
        grid_observed_seconds=grid.observed_seconds,
        grid_reconstructed_seconds=grid.reconstructed_seconds,
        grid_samples=grid.samples,
        source_wh=source_wh,
        source_gco2=source_gco2,
        avoided=avoided,
        hours=_hour_slots(buckets),
        grid_hours=_grid_hour_slots(grid_samples),
        days_with_data=len({day for day, _ in buckets}),
    )
