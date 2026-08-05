import logging
import platform

from ecomonitor.sensors.base import SensorBackend, SensorUnavailableError
from ecomonitor.sensors.fallback_model import FallbackModelBackend

logger = logging.getLogger(__name__)


def get_backend() -> SensorBackend:
    #Prova il backend nativo della piattaforma; se non e' disponibile (permessi
    #mancanti, driver assente, non implementato) ripiega sulla stima TDP, che
    #funziona sempre ma restituisce sempre is_estimated=True
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
        logger.warning("Backend nativo (%s) non disponibile: %s — uso il ripiego TDP", system, exc)

    return FallbackModelBackend()
