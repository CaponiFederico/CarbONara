from dataclasses import dataclass
from datetime import datetime

#Campionare e persistere sono decisioni diverse: la misura ha bisogno di 5 s,
#la persistenza no (a 5 s sono ~17 000 righe e ~2,4 MB al giorno)

#I tick vengono sommati in una riga al minuto

#La finestra di aggregazione si chiude quando:
# - il tick corrente e' di regime diverso dal precedente (plugged, attribution_source)
# - il tick corrente e' il primo dopo un buco di campionamento (starts_new_row)
# - la finestra ha superato il tempo massimo (flush_interval_seconds)

DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class EnergySample:
    #un tick, prima dell'aggregazione

    timestamp: datetime
    wh: float
    gco2: float
    intensity_g_per_kwh: float
    is_plugged: bool
    is_estimated: bool
    attribution_source: str
    battery_percent: float | None
    charged_percent: float
    discharged_percent: float
    pool_coverage_ratio: float
    battery_assisted: bool
    starts_new_row: bool = False
    #True quando il tick segue una finestra non osservata: il pending va chiuso
    #prima, cosi' nessuna riga attraversa il buco


@dataclass
class BufferedEnergyRow:
    #riga pronta per LocalDatabase.record_buffered_energy_row()

    timestamp: datetime
    wh: float
    gco2: float
    intensity_g_per_kwh: float
    is_plugged: bool
    is_estimated: bool
    attribution_source: str
    battery_percent: float | None
    charged_percent: float
    discharged_percent: float
    pool_coverage_ratio: float
    battery_assisted: bool
    n_samples: int
    started_at: datetime


class EnergyEventBuffer:
    def __init__(self, flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS) -> None:
        self.flush_interval_seconds = flush_interval_seconds
        self._started_at: datetime | None = None
        self._last: EnergySample | None = None
        self._n = 0
        self._wh = 0.0
        self._gco2 = 0.0
        self._charged = 0.0
        self._discharged = 0.0
        self._covered = 0.0
        self._requested = 0.0
        self._estimated = False
        self._assisted = False

    # -- stato in sospeso --------------------------------------------------

    @property
    def pending_wh(self) -> float:
        #energia non ancora scritta: i totali del giorno la sommano, altrimenti
        #resterebbero fermi fino al prossimo flush
        return self._wh

    @property
    def pending_gco2(self) -> float:
        return self._gco2

    # -- accumulo ----------------------------------------------------------

    def add(self, sample: EnergySample) -> BufferedEnergyRow | None:
        #ritorna la riga da scrivere quando la finestra si chiude
        completed: BufferedEnergyRow | None = None

        if self._n > 0 and (sample.starts_new_row or self._regime_changed(sample)):
            completed = self.flush()

        self._accumulate(sample)

        if completed is None and self._elapsed_seconds() >= self.flush_interval_seconds:
            completed = self.flush()

        return completed

    def flush(self) -> BufferedEnergyRow | None:
        #chiude la finestra corrente, None se non c'e' niente in sospeso
        if self._n == 0 or self._last is None or self._started_at is None:
            return None

        #intensita' come media pesata sull'energia: mantiene esatta l'identita'
        #gco2 == wh / 1000 * intensity anche sulla riga aggregata
        intensity = (
            self._gco2 / self._wh * 1000.0 if self._wh > 0 else self._last.intensity_g_per_kwh
        )
        #copertura come rapporto sull'intera finestra, non media dei rapporti:
        #peserebbe allo stesso modo un tick da 10 punti e uno da 0,1
        coverage = self._covered / self._requested if self._requested > 0 else 1.0

        row = BufferedEnergyRow(
            timestamp=self._last.timestamp,
            wh=self._wh,
            gco2=self._gco2,
            intensity_g_per_kwh=intensity,
            is_plugged=self._last.is_plugged,
            is_estimated=self._estimated,
            attribution_source=self._last.attribution_source,
            battery_percent=self._last.battery_percent,  # e' uno stato, non un flusso
            charged_percent=self._charged,
            discharged_percent=self._discharged,
            pool_coverage_ratio=coverage,
            battery_assisted=self._assisted,
            n_samples=self._n,
            started_at=self._started_at,
        )
        self._reset()
        return row

    # -- interni -----------------------------------------------------------

    def _regime_changed(self, sample: EnergySample) -> bool:
        assert self._last is not None
        return (
            sample.is_plugged != self._last.is_plugged
            or sample.attribution_source != self._last.attribution_source
        )

    def _elapsed_seconds(self) -> float:
        if self._started_at is None or self._last is None:
            return 0.0
        return (self._last.timestamp - self._started_at).total_seconds()

    def _accumulate(self, sample: EnergySample) -> None:
        if self._n == 0:
            self._started_at = sample.timestamp
        self._n += 1
        self._last = sample
        self._wh += sample.wh
        self._gco2 += sample.gco2
        self._charged += sample.charged_percent
        self._discharged += sample.discharged_percent
        self._requested += sample.discharged_percent
        self._covered += sample.discharged_percent * sample.pool_coverage_ratio
        #stimata o assistita in almeno un tick: marcare come misurata una
        #finestra in parte stimata sarebbe l'errore nella direzione sbagliata
        self._estimated = self._estimated or sample.is_estimated
        self._assisted = self._assisted or sample.battery_assisted

    def _reset(self) -> None:
        self._started_at = None
        self._last = None
        self._n = 0
        self._wh = 0.0
        self._gco2 = 0.0
        self._charged = 0.0
        self._discharged = 0.0
        self._covered = 0.0
        self._requested = 0.0
        self._estimated = False
        self._assisted = False
