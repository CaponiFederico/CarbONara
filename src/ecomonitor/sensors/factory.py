import logging
import platform

from ecomonitor.sensors.base import SensorBackend, SensorUnavailableError
from ecomonitor.sensors.calibration import (
    calibration_from_database,
    cpu_calibration_from_database,
)
from ecomonitor.sensors.fallback_model import CalibratedFallbackBackend, FallbackModelBackend

logger = logging.getLogger(__name__)


def get_backend(db=None) -> SensorBackend:
    #Ritorna il backend nativo se disponibile, altrimenti un backend di ripiego
    system = platform.system()

    try:
        if system == "Linux":
            from ecomonitor.sensors.linux_rapl import LinuxRaplBackend

            return LinuxRaplBackend()
        if system == "Windows":
            from ecomonitor.sensors.windows_lhm import WindowsLhmBackend

            return WindowsLhmBackend()
        if system == "Darwin":
            from ecomonitor.sensors.macos_powermetrics import MacosPowermetricsBackend

            return MacosPowermetricsBackend()
    except SensorUnavailableError as exc:
        logger.warning("Backend nativo (%s) non disponibile: %s", system, exc)

    if system == "Windows":
        #Senza privilegi la GPU si legge lo stesso
        try:
            from ecomonitor.sensors.windows_lhm import WindowsPartialLhmBackend

            cpu_calibration = (
                cpu_calibration_from_database(db) if db is not None else None
            )
            backend = WindowsPartialLhmBackend(cpu_calibration=cpu_calibration)
            logger.warning(
                "CPU non leggibile senza privilegi: uso la GPU misurata e stimo la CPU (%s). "
                "Per una misura completa serve un processo elevato",
                cpu_calibration.describe() if cpu_calibration else "TDP generico",
            )
            return backend
        except SensorUnavailableError as exc:
            logger.info("Nessuna misura parziale disponibile: %s", exc)

    if db is not None:
        calibration = calibration_from_database(db)
        if calibration is not None:
            return CalibratedFallbackBackend(calibration)

    logger.warning(
        "Nessuno storico misurato su cui calibrare: uso la stima a TDP generico"
    )
    return FallbackModelBackend()
