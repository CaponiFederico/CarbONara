import psutil

from ecomonitor.sensors.base import PowerReading, now_utc

DEFAULT_TDP_W = 15.0  # TDP tipico di un ultrabook

#Stima usata quando nessun sensore hardware e' disponibile (permessi mancanti,
#driver assente)

class FallbackModelBackend:
    def __init__(self, tdp_w: float = DEFAULT_TDP_W) -> None:
        self.tdp_w = tdp_w
        #la prima chiamata a cpu_percent con interval=None vale sempre 0.0
        psutil.cpu_percent(interval=None)

    def read(self) -> PowerReading:
        cpu_fraction = psutil.cpu_percent(interval=None) / 100.0
        return PowerReading(
            package_w=self.tdp_w * cpu_fraction,
            dram_w=None,
            gpu_integrated_w=None,
            gpu_dedicated_w=None,
            timestamp=now_utc(),
            is_estimated=True,
        )
