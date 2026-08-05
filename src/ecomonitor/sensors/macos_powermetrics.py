from ecomonitor.sensors.base import PowerReading, SensorUnavailableError

#Backend macOS, non implementato: powermetrics richiede sudo 

class MacosPowermetricsBackend:
    def __init__(self) -> None:
        raise SensorUnavailableError(
            "Backend powermetrics non ancora implementato (richiede sudo e "
            "verifica su hardware Apple reale)"
        )

    def is_available(self) -> bool:
        return False

    def read(self) -> PowerReading:
        raise SensorUnavailableError("MacosPowermetricsBackend non implementato")
