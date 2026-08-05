from ecomonitor.sensors.base import PowerReading, SensorUnavailableError

#Backend macOS, non implementato: powermetrics richiede sudo (accede a contatori
#del PMU riservati al kernel) e non e' verificabile senza hardware Apple.
#Schema previsto:
#
#    result = subprocess.run(
#        ["sudo", "powermetrics", "--samplers", "cpu_power,gpu_power",
#         "-i", "1000", "-n", "1", "--format", "plist"],
#        capture_output=True, timeout=5,
#    )
#    data = plistlib.loads(result.stdout)
#    package_w = data["processor"]["package_watts"]
#    gpu_integrated_w = data["processor"].get("gpu_watts")
#
#Su Apple Silicon la GPU e' nel SoC, quindi va in gpu_integrated_w e
#gpu_dedicated_w resta None. Chiedere sudo a ogni tick non e' praticabile per
#un'app continua: servirebbe un helper privilegiato residente


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
