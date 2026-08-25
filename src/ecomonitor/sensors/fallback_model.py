import logging

from ecomonitor.sensors.base import PowerReading, now_utc, sample_cpu_percent
from ecomonitor.sensors.calibration import PowerCalibration

DEFAULT_TDP_W = 15.0  # TDP tipico di un ultrabook

#Stime usate quando nessun sensore hardware e' disponibile 
logger = logging.getLogger(__name__)


class FallbackModelBackend:
    #Ripiego di ultima istanza: TDP costante scalato sull'utilizzo di CPU

    def __init__(self, tdp_w: float = DEFAULT_TDP_W) -> None:
        self.tdp_w = tdp_w
        #la prima chiamata a cpu_percent con interval=None vale sempre 0.0
        sample_cpu_percent()

    def read(self) -> PowerReading:
        cpu_percent = sample_cpu_percent() or 0.0
        return PowerReading(
            package_w=self.tdp_w * (cpu_percent / 100.0),
            dram_w=None,
            gpu_integrated_w=None,
            gpu_dedicated_w=None,
            timestamp=now_utc(),
            is_estimated=True,
            cpu_percent=cpu_percent,
        )


class CalibratedFallbackBackend:
    #Ripiego calibrato: P = riposo + pendenza * utilizzo, con i coefficienti
    #ricavati dalle righe misurate di questa macchina

    def __init__(self, calibration: PowerCalibration) -> None:
        self.calibration = calibration
        sample_cpu_percent()
        logger.info("Ripiego calibrato su questa macchina: %s", calibration.describe())

    def read(self) -> PowerReading:
        cpu_percent = sample_cpu_percent() or 0.0
        return PowerReading(
            package_w=self.calibration.predict(cpu_percent),
            dram_w=None,
            gpu_integrated_w=None,
            gpu_dedicated_w=None,
            timestamp=now_utc(),
            is_estimated=True,
            cpu_percent=cpu_percent,
        )
