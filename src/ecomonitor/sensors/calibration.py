from __future__ import annotations

import logging
from dataclasses import dataclass

#Calibrazione del modello di ripiego sullo storico della macchina.

logger = logging.getLogger(__name__)

#Sotto questo numero di righe la retta non ha abbastanza punti 
# per essere affidabile. Con meno di 30 righe, la pendenza e' 
# troppo sensibile a un singolo punto anomalo
MIN_SAMPLES = 30

#Distanza fra l'utilizzo piu' basso e quello piu' alto
#perche' la pendenza sia identificabile
MIN_CPU_SPREAD = 15.0  # punti percentuali

#Questo e' il coefficiente di determinazione minimo perche' la retta
#sia considerata un modello valido. Sotto questo valore, la retta
#non spiega abbastanza della varianza dei dati, e il modello di ripiego
#non e' affidabile
MIN_R_SQUARED = 0.5

#Oltre questo una riga media su condizioni troppo diverse
MAX_ROW_SECONDS = 900.0


@dataclass(frozen=True)
class PowerCalibration:
    #I coefficienti del modello di ripiego sullo storico della macchina.
    #La retta e' idle_w + slope_w * (cpu_percent / 100.0)
    #La retta puo' essere valida o meno, e puo' essere una retta con pendenza
    #  o un livello medio
    idle_w: float
    slope_w: float  # watt aggiuntivi al 100% di utilizzo
    n_samples: int
    cpu_spread: float
    r_squared: float | None
    #"linear" quando la retta regge
    #"level" quando si e' potuto tenere solo illivello medio
    kind: str = "linear"
    reason: str = ""

    def predict(self, cpu_percent: float) -> float:
        return max(0.0, self.idle_w + self.slope_w * (cpu_percent / 100.0))

    @property
    def has_slope(self) -> bool:
        return self.slope_w > 0.0

    def describe(self) -> str:
        if self.kind == "level":
            base = f"livello medio {self.idle_w:.1f} W (su {self.n_samples} righe misurate"
            if self.reason:
                base += f", {self.reason}"
            return base + ")"

        base = (
            f"{self.idle_w:.1f} W a riposo + {self.slope_w:.1f} W al 100% di CPU "
            f"(su {self.n_samples} righe misurate"
        )
        if self.r_squared is not None:
            base += f", R²={self.r_squared:.2f}"
        return base + ")"


def _level_only(watts: list[float], spread: float, reason: str) -> PowerCalibration:
    #la retta non regge, ma il livello medio di questa macchina resta di ordini 
    #di grandezza piu' vicino al verorispetto ad un ripiego statico 
    mean_w = sum(watts) / len(watts)
    calibration = PowerCalibration(
        idle_w=mean_w, slope_w=0.0, n_samples=len(watts), cpu_spread=spread,
        r_squared=None, kind="level", reason=reason,
    )
    logger.info("Calibrazione: %s", calibration.describe())
    return calibration


def fit_calibration(samples: list[tuple[float, float]]) -> PowerCalibration | None:
    #samples: coppie (utilizzo_cpu_percento, potenza_media_w) da righe misurate.
    #None quando i dati non bastano a sostenere un modello
    usable = [
        (cpu, watts)
        for cpu, watts in samples
        if cpu is not None and watts is not None and 0.0 <= cpu <= 100.0 and watts > 0.0
    ]
    if len(usable) < MIN_SAMPLES:
        logger.debug(
            "Calibrazione: %d righe utili, ne servono %d", len(usable), MIN_SAMPLES
        )
        return None

    cpus = [cpu for cpu, _ in usable]
    watts = [w for _, w in usable]
    spread = max(cpus) - min(cpus)

    n = float(len(usable))
    mean_cpu = sum(cpus) / n
    mean_w = sum(watts) / n

    if spread < MIN_CPU_SPREAD:
        #Utilizzo troppo uniforme per separare pendenza e intercetta
        return _level_only(
            watts, spread, f"utilizzo troppo uniforme, {spread:.1f} punti di escursione"
        )

    #minimi quadrati ordinari
    sxx = sum((cpu - mean_cpu) ** 2 for cpu in cpus)
    sxy = sum((cpu - mean_cpu) * (w - mean_w) for cpu, w in usable)
    if sxx <= 0:
        return None

    slope_per_point = sxy / sxx
    intercept = mean_w - slope_per_point * mean_cpu

    sst = sum((w - mean_w) ** 2 for w in watts)
    r_squared = None
    if sst > 0:
        ssr = sum(
            (w - (intercept + slope_per_point * cpu)) ** 2 for cpu, w in usable
        )
        r_squared = max(0.0, 1.0 - ssr / sst)

    #pendenza negativa la macchina consuma meno sotto carico
    if slope_per_point < 0:
        return _level_only(
            watts, spread, f"pendenza negativa, {slope_per_point * 100.0:.0f} W al 100%"
        )

    #intercetta negativa consumerebbe meno di zero
    if intercept < 0:
        return _level_only(
            watts, spread, f"intercetta negativa, {intercept:.0f} W a riposo"
        )

    #La retta non spiega abbastanza della varianza dei dati, e il modello di ripiego
    #non e' affidabile. In quel caso si usa il livello medio
    if r_squared is not None and r_squared < MIN_R_SQUARED:
        return _level_only(
            watts,
            spread,
            f"la retta su cpu% spiega solo il {r_squared * 100:.0f}% della varianza",
        )

    calibration = PowerCalibration(
        idle_w=intercept,
        slope_w=slope_per_point * 100.0,
        n_samples=len(usable),
        cpu_spread=spread,
        r_squared=r_squared,
    )
    logger.info("Calibrazione del ripiego: %s", calibration.describe())
    return calibration


def cpu_calibration_from_database(db) -> PowerCalibration | None:
    #Calibrazione del dominio CPU
    try:
        rows = db.measured_cpu_samples(max_row_seconds=MAX_ROW_SECONDS)
    except Exception:
        logger.exception("Calibrazione CPU: lettura dello storico non riuscita")
        return None
    return fit_calibration(rows)


def calibration_from_database(db) -> PowerCalibration | None:
    #Le righe misurate che portano anche l'utilizzo di CPU. Le righe scritte
    #prima che quella colonna esistesse non hanno l'ascissa e restano fuori
    try:
        rows = db.measured_power_samples(max_row_seconds=MAX_ROW_SECONDS)
    except Exception:
        logger.exception("Calibrazione: lettura dello storico non riuscita")
        return None
    return fit_calibration(rows)
