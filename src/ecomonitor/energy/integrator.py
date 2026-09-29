from ecomonitor.sensors.base import PowerReading

#Integra la potenza (W) in energia (Wh) col metodo dei trapezi. Gli intervalli
#troppo lunghi vengono scartati, non integrati

DEFAULT_MAX_INTERVAL_SECONDS = 60.0
#12x il polling di default: un tick in ritardo resta integrato, una
#sospensione no. L'Orchestrator lo ricalcola dall'intervallo configurato

DEFAULT_MAX_PLAUSIBLE_W = 1000.0
#Tetto di plausibilita' su CPU+GPU


class EnergyIntegrator:
    def __init__(
        self,
        max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS,
        max_plausible_w: float = DEFAULT_MAX_PLAUSIBLE_W,
    ) -> None:
        self.max_interval_seconds = max_interval_seconds
        self.max_plausible_w = max_plausible_w
        self._last_reading: PowerReading | None = None
        self.skipped_gap_seconds: float = 0.0  # durata scartata dall'ultima chiamata

        self.last_interval_seconds: float = 0.0
        #Durata dell'intervallo effettivamente integrato dall'ultima chiamata
        #0.0 se la lettura e' stata scartata

        self.last_package_wh: float = 0.0
        #Wh dell'ultimo intervallo attribuibile al solo dominio CPU

    def add_reading(self, reading: PowerReading) -> float:
        #Ritorna i Wh consumati dall'ultima lettura (0.0 alla prima: non c'e'
        #ancora un intervallo)
        self.skipped_gap_seconds = 0.0
        self.last_interval_seconds = 0.0
        self.last_package_wh = 0.0

        previous = self._last_reading
        delta_seconds = (
            (reading.timestamp - previous.timestamp).total_seconds()
            if previous is not None
            else 0.0
        )

        #Finestra non osservata (sospensione, applicazione chiusa)
        crosses_gap = previous is not None and delta_seconds > self.max_interval_seconds
        if crosses_gap:
            self.skipped_gap_seconds = delta_seconds

        #Lettura fuori scala
        if not self._is_plausible(reading):
            self._last_reading = None
            return 0.0

        if previous is None:
            self._last_reading = reading
            return 0.0

        if delta_seconds <= 0:
            #timestamp non monotono ignora l'intervallo
            #La lettura resta valida come baseline, e' solo l'ordine 
            #a essere sbagliato
            self._last_reading = reading
            return 0.0

        if crosses_gap:
            #La lettura che chiude la finestra NON diventa la nuova baseline
            self._last_reading = None
            return 0.0

        avg_w = (reading.total_w + previous.total_w) / 2.0
        delta_wh = avg_w * (delta_seconds / 3600.0)

        avg_package_w = (reading.package_w + previous.package_w) / 2.0
        self.last_package_wh = avg_package_w * (delta_seconds / 3600.0)

        self.last_interval_seconds = delta_seconds
        self._last_reading = reading
        return delta_wh

    def _is_plausible(self, reading: PowerReading) -> bool:
        #Una potenza negativa e' impossibile, una sopra il tetto e' un artefatto
        #del contatore: in entrambi i casi la lettura non e' una misura
        total_w = reading.total_w
        return 0.0 <= total_w <= self.max_plausible_w
