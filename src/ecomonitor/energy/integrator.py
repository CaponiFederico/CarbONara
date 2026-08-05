from ecomonitor.sensors.base import PowerReading

#Integra la potenza (W) in energia (Wh) col metodo dei trapezi. Gli intervalli
#troppo lunghi vengono scartati, non integrati: dopo una sospensione la
#macchina non ha consumato nulla, ma il trapezio conterebbe ~160 Wh inventati
#su 8 ore. La finestra e' esclusa dal conteggio, non stimata, e
#skipped_gap_seconds la rende osservabile

DEFAULT_MAX_INTERVAL_SECONDS = 60.0
#12x il polling di default: un tick in ritardo resta integrato, una
#sospensione no. L'Orchestrator lo ricalcola dall'intervallo configurato


class EnergyIntegrator:
    def __init__(self, max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS) -> None:
        self.max_interval_seconds = max_interval_seconds
        self._last_reading: PowerReading | None = None
        self.cumulative_wh: float = 0.0
        self.skipped_gap_seconds: float = 0.0  # durata scartata dall'ultima chiamata
        self.skipped_intervals: int = 0
        self.total_skipped_seconds: float = 0.0

    def add_reading(self, reading: PowerReading) -> float:
        #Ritorna i Wh consumati dall'ultima lettura (0.0 alla prima: non c'e'
        #ancora un intervallo)
        self.skipped_gap_seconds = 0.0

        if self._last_reading is None:
            self._last_reading = reading
            return 0.0

        delta_seconds = (reading.timestamp - self._last_reading.timestamp).total_seconds()
        if delta_seconds <= 0:
            #timestamp non monotono (orologio di sistema modificato): ignora
            self._last_reading = reading
            return 0.0

        if delta_seconds > self.max_interval_seconds:
            #sospensione: nessun consumo da attribuire, la lettura corrente
            #diventa la nuova baseline
            self.skipped_gap_seconds = delta_seconds
            self.skipped_intervals += 1
            self.total_skipped_seconds += delta_seconds
            self._last_reading = reading
            return 0.0

        avg_w = (reading.total_w + self._last_reading.total_w) / 2.0
        delta_wh = avg_w * (delta_seconds / 3600.0)

        self.cumulative_wh += delta_wh
        self._last_reading = reading
        return delta_wh

    def reset(self) -> None:
        self._last_reading = None
        self.cumulative_wh = 0.0
        self.skipped_gap_seconds = 0.0
        self.skipped_intervals = 0
        self.total_skipped_seconds = 0.0
