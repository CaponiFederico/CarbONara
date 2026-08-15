from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True)
class PowerReading:
    #Lettura istantanea di potenza. Watt, None se il dominio non e' disponibile
    #su questo hardware. Lo scope e' CPU+GPU: schermo, ventole, disco e chipset
    #non sono misurabili con questi sensori e restano fuori dal conteggio

    package_w: float
    dram_w: float | None
    gpu_integrated_w: float | None
    #GPU integrata e dedicata sono campi separati e non un unico gpu_w: su una
    #macchina con entrambe attive sommarle confonderebbe due dispositivi
    #fisicamente distinti, con dinamiche di utilizzo diverse
    gpu_dedicated_w: float | None
    timestamp: datetime
    is_estimated: bool  # True se stima di ripiego (TDP), non sensore reale

    @property
    def total_w(self) -> float:
        return (
            self.package_w
            + (self.dram_w or 0.0)
            + (self.gpu_integrated_w or 0.0)
            + (self.gpu_dedicated_w or 0.0)
        )


class SensorUnavailableError(RuntimeError):
    #Il backend non riesce ad accedere al sensore hardware: permessi mancanti,
    #driver assente, piattaforma non supportata
    pass


class SensorBackend(Protocol):
    #Un backend implementa read(). Deve essere veloce (qualche decina di ms al
    #massimo): viene chiamato a ogni tick di polling

    def read(self) -> PowerReading: ...


def now_utc() -> datetime:
    return datetime.now(UTC)
