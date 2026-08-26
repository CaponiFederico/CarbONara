from ecomonitor.sensors.base import NATIVE_MODE, PowerReading, SensorUnavailableError

#Backend macOS, non implementato: powermetrics richiede sudo 

class MacosPowermetricsBackend:
    mode = NATIVE_MODE
    def __init__(self) -> None:
        raise SensorUnavailableError(
            "Backend powermetrics non ancora implementato (richiede sudo e "
            "verifica su hardware Apple reale)"
        )

    def read(self) -> PowerReading:
        raise SensorUnavailableError("MacosPowermetricsBackend non implementato")
