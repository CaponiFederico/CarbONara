from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import psutil


@dataclass(frozen=True)
class PowerReading:
    #Lettura istantanea di potenza. Watt, None se il dominio non e' disponibile
    #Lo scope e' CPU+GPU

    package_w: float
    dram_w: float | None
    gpu_integrated_w: float | None
    #GPU integrata e dedicata sono campi separati e non un unico gpu_w
    gpu_dedicated_w: float | None
    timestamp: datetime
    is_estimated: bool  # True se stima di ripiego, non sensore reale

    cpu_percent: float | None = None
    #Utilizzo di CPU dell'istante

    @property
    def total_w(self) -> float:
        return (
            self.package_w
            + (self.dram_w or 0.0)
            + (self.gpu_integrated_w or 0.0)
            + (self.gpu_dedicated_w or 0.0)
        )


@dataclass(frozen=True)
class MeasurementMode:
    kind: str  # native | partial | calibrated | generic
    detail: str  # cosa e' misurato e cosa e' stimato, in una riga
    advice: str | None = None  # azione utile, None quando non c'e' niente da fare

    @property
    def is_complete(self) -> bool:
        return self.kind == "native"


NATIVE_MODE = MeasurementMode(
    kind="native",
    detail="misura diretta dai sensori hardware",
)


class SensorUnavailableError(RuntimeError):
    #Il backend non riesce ad accedere al sensore hardware: permessi mancanti,
    #driver assente, piattaforma non supportata
    pass


class SensorBackend(Protocol):
    mode: MeasurementMode
    #Un backend implementa read()  viene chiamato a ogni tick di polling

    def read(self) -> PowerReading: ...


def now_utc() -> datetime:
    return datetime.now(UTC)


def sample_cpu_percent() -> float | None:
    #Va chiamata una volta per tick, e la chiama il backend attivo.
    #Ritorna None se non e' disponibile il sensore di CPU
    try:
        return float(psutil.cpu_percent(interval=None))
    except Exception:  # psutil puo' fallire
        return None
