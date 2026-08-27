import logging
import platform

from ecomonitor.sensors.base import (
    NATIVE_MODE,
    MeasurementMode,
    SensorBackend,
    SensorUnavailableError,
)

#Azione che sblocca la misura vera
_ELEVATE = "Avvia l'app come amministratore per misurare la CPU invece di stimarla."
_ELEVATE_ONCE = (
    "Avviala una volta come amministratore: l'app impara il consumo di questa "
    "macchina e da allora le stime saranno molto piu' vicine al vero."
)


def _tagged(backend: SensorBackend, mode: MeasurementMode) -> SensorBackend:
    #il livello di misura viaggia col backend
    backend.mode = mode
    return backend


def _mode_for(kind: str, calibration) -> MeasurementMode:
    #La descrizione dice cosa e' misurato, il consiglio dice cosa manca
    calibrated = calibration is not None
    detail = {
        "partial": "GPU misurata, CPU stimata",
        "calibrated": "stima su tutti i domini",
        "generic": "stima su tutti i domini",
    }[kind]
    if calibrated:
        detail += ", tarata su questa macchina"
    elif kind == "generic":
        detail += " da TDP generico"

    return MeasurementMode(
        kind=kind, detail=detail, advice=_ELEVATE if calibrated else _ELEVATE_ONCE
    )
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

            return _tagged(LinuxRaplBackend(), NATIVE_MODE)
        if system == "Windows":
            from ecomonitor.sensors.windows_lhm import WindowsLhmBackend

            return _tagged(WindowsLhmBackend(), NATIVE_MODE)
        if system == "Darwin":
            from ecomonitor.sensors.macos_powermetrics import MacosPowermetricsBackend

            return _tagged(MacosPowermetricsBackend(), NATIVE_MODE)
    except SensorUnavailableError as exc:
        logger.warning("Backend nativo (%s) non disponibile: %s", system, exc)

    if system == "Windows":
        #Senza privilegi la GPU si legge lo stesso
        try:
            from ecomonitor.sensors.windows_lhm import WindowsPartialLhmBackend

            cpu_calibration = (
                cpu_calibration_from_database(db) if db is not None else None
            )
            backend = _tagged(
                WindowsPartialLhmBackend(cpu_calibration=cpu_calibration),
                _mode_for("partial", cpu_calibration),
            )
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
            return _tagged(
                CalibratedFallbackBackend(calibration),
                _mode_for("calibrated", calibration),
            )

    logger.warning(
        "Nessuno storico misurato su cui calibrare: uso la stima a TDP generico"
    )
    return _tagged(FallbackModelBackend(), _mode_for("generic", None))
