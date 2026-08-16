from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# Sotto questa soglia e' rumore di arrotondamento del sistema operativo
MIN_SIGNIFICANT_DELTA_PERCENT = 1e-6

class AttributionModel(Enum):
    WEIGHTED_AVERAGE = "weighted_average"
    FIFO = "fifo"

@dataclass(frozen=True)
class DischargeOutcome:
    #esito di un prelievo di carica dal pool, con copertura parziale

    intensity_g_per_kwh: float
    requested_percent: float
    covered_percent: float

    @property
    def coverage_ratio(self) -> float:
        #Frazione del prelievo effettivamente coperta dallo storico (0..1)

        if self.requested_percent <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return 1.0
        return min(1.0, self.covered_percent / self.requested_percent)

@dataclass
class BatteryCarbonPool:
    model: AttributionModel = AttributionModel.WEIGHTED_AVERAGE
    stored_percent: float = 0.0
    avg_intensity_g_per_kwh: float = 0.0
    _fifo_layers: deque[tuple[float, float]] = field(default_factory=deque)  # (percent, intensity)

    @property
    def has_history(self) -> bool:
        #True se il pool sa  con quale mix e'stata caricata la batteria
        #False l'attribuzione deve ricadere su un valore di riferimento esterno 
        
        if self.stored_percent <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return False
        if self.model is AttributionModel.FIFO:
            return bool(self._fifo_layers)
        return self.avg_intensity_g_per_kwh > 0.0

    @property
    def current_intensity_g_per_kwh(self) -> float:
        #Tick in cui la percentuale di batteria non e'cambiata
        #ma e' comunque presente consumo di CPU/GPU 
        if self.model is AttributionModel.FIFO:
            return self._fifo_layers[0][1] if self._fifo_layers else 0.0
        return self.avg_intensity_g_per_kwh

    def charge(self, delta_percent: float, grid_intensity_g_per_kwh: float) -> None:
        #Registra delta_percent punti di carica entrati in batteria mentre
        #la rete aveva intensita' grid_intensity_g_per_kwh
        if delta_percent <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return
        if self.model is AttributionModel.WEIGHTED_AVERAGE:
            total = self.stored_percent + delta_percent
            self.avg_intensity_g_per_kwh = (
                self.stored_percent * self.avg_intensity_g_per_kwh
                + delta_percent * grid_intensity_g_per_kwh
            ) / total
            self.stored_percent = total
        else:  # FIFO
            self._fifo_layers.append((delta_percent, grid_intensity_g_per_kwh))
            self.stored_percent += delta_percent

    def discharge(self, delta_percent: float) -> float:
        #Come discharge_with_coverage(), ma ritorna la sola intensita
        return self.discharge_with_coverage(delta_percent).intensity_g_per_kwh

    def discharge_with_coverage(self, delta_percent: float) -> DischargeOutcome:
        #Consuma delta_percent punti di carica dal pool e ritorna
        #l'intensita' di carbonio(gCO2/kWh) dell'energia prelevata
        #e quanti punti erano davvero tracciati 

        requested = max(0.0, delta_percent)

        if requested <= MIN_SIGNIFICANT_DELTA_PERCENT:
            # La % non si e' mossa in questo tick, nessun prelievo da coprire,
            # ma il consumo di CPU/GPU va attribuito comunque.
            return DischargeOutcome(self.current_intensity_g_per_kwh, requested, requested)

        available = min(requested, self.stored_percent)
        if available <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return DischargeOutcome(self.current_intensity_g_per_kwh, requested, 0.0)

        if self.model is AttributionModel.WEIGHTED_AVERAGE:
            self.stored_percent -= available
            # avg_intensity_g_per_kwh non cambia: si consuma un pool omogeneo
            return DischargeOutcome(self.avg_intensity_g_per_kwh, requested, available)

        # FIFO: consuma dai layer di carica piu' vecchi, e ritorna la media
        # pesata dei layer effettivamente toccati da questo prelievo
        remaining = available
        weighted_sum = 0.0
        consumed_total = 0.0
        while remaining > MIN_SIGNIFICANT_DELTA_PERCENT and self._fifo_layers:
            layer_percent, layer_intensity = self._fifo_layers[0]
            consumed = min(remaining, layer_percent)
            weighted_sum += consumed * layer_intensity
            consumed_total += consumed
            remaining -= consumed
            layer_percent -= consumed
            if layer_percent <= MIN_SIGNIFICANT_DELTA_PERCENT:
                self._fifo_layers.popleft()
            else:
                self._fifo_layers[0] = (layer_percent, layer_intensity)

        #se non ci sono layer, ma stored_percent > 0, la carica residua e' stata
        #persa
        self.stored_percent = max(0.0, self.stored_percent - consumed_total)
        if consumed_total <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return DischargeOutcome(self.current_intensity_g_per_kwh, requested, 0.0)
        return DischargeOutcome(weighted_sum / consumed_total, requested, consumed_total)

    def switch_model(self, model: AttributionModel) -> bool:
        #Cambia modello di attribuzione conservando la carica gia' tracciata
        #Ritorna True se il modello e' cambiato
        if model is self.model:
            return False

        if model is AttributionModel.FIFO:
            self._fifo_layers = deque()
            if self.stored_percent > MIN_SIGNIFICANT_DELTA_PERCENT:
                self._fifo_layers.append((self.stored_percent, self.avg_intensity_g_per_kwh))
        else:
            total = sum(percent for percent, _ in self._fifo_layers)
            if total > MIN_SIGNIFICANT_DELTA_PERCENT:
                self.avg_intensity_g_per_kwh = (
                    sum(percent * intensity for percent, intensity in self._fifo_layers) / total
                )
                self.stored_percent = total
            else:
                #Nessun layer da convertire
                
                #Se stored_percent dichiarava comunque della carica, quella carica 
                #non ha un'intensita' associata
                self.stored_percent = 0.0
                self.avg_intensity_g_per_kwh = 0.0
            self._fifo_layers = deque()

        self.model = model
        return True

    def reset(self) -> None:
        #Svuota il pool conservando il modello
        self.stored_percent = 0.0
        self.avg_intensity_g_per_kwh = 0.0
        self._fifo_layers = deque()

    #Il pool deve essere serializzabile per poter essere salvato tra un tick e l'altro

    def to_dict(self) -> dict:
        return {
            "model": self.model.value,
            "stored_percent": self.stored_percent,
            "avg_intensity_g_per_kwh": self.avg_intensity_g_per_kwh,
            "fifo_layers": [list(layer) for layer in self._fifo_layers],
        }

    @classmethod
    def from_dict(cls, data: dict) -> BatteryCarbonPool:
        return cls(
            model=AttributionModel(data["model"]),
            stored_percent=float(data["stored_percent"]),
            avg_intensity_g_per_kwh=float(data["avg_intensity_g_per_kwh"]),
            _fifo_layers=deque(
                (float(percent), float(intensity))
                for percent, intensity in data.get("fifo_layers", [])
            ),
        )
