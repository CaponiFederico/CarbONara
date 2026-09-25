import logging
import math
import os
import plistlib
import shlex
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from ecomonitor.sensors.base import (
    NATIVE_MODE,
    PowerReading,
    SensorLostError,
    SensorUnavailableError,
    now_utc,
    sample_cpu_percent,
)

logger = logging.getLogger(__name__)

POWERMETRICS_PATH = "/usr/bin/powermetrics"
SUDO_PATH = "/usr/bin/sudo"
SAMPLE_INTERVAL_MS = 1000
SAMPLERS = "cpu_power,gpu_power"

#Argomenti
POWERMETRICS_ARGUMENTS: tuple[str, ...] = (
    "-s", 
    SAMPLERS, 
    "-f",   
    "plist",  
    "-i",  
    str(SAMPLE_INTERVAL_MS),
)

PERMISSIONS_SCRIPT = "install_powermetrics_permissions.sh"

STARTUP_TIMEOUT_SECONDS = 8.0
STALE_AFTER_SECONDS = 5.0
#riavvi di fila senza un campione prima di arrendersi al ripiego
MAX_RESTARTS_WITHOUT_SAMPLE = 3

MAX_BUFFER_BYTES = 1_000_000
MAX_PLAUSIBLE_W = 1000.0

_PLIST_END = b"</plist>"
_READ_CHUNK_BYTES = 65536
_STDERR_LINES = 20


class PowermetricsFormatError(ValueError):
    pass


@dataclass(frozen=True)
class PowermetricsSample:
    elapsed_seconds: float
    cpu_w: float
    gpu_w: float | None
    ane_w: float | None


def powermetrics_command(running_as_root: bool, path: str = POWERMETRICS_PATH) -> list[str]:
    if running_as_root:
        return [path, *POWERMETRICS_ARGUMENTS]
    return [SUDO_PATH, "-n", path, *POWERMETRICS_ARGUMENTS] 


def _sudoers_escape(argument: str) -> str:
    for character in ("\\", ",", ":", "="):
        argument = argument.replace(character, "\\" + character)
    return argument


def sudoers_command_spec() -> str: 
    arguments = " ".join(_sudoers_escape(argument) for argument in POWERMETRICS_ARGUMENTS)
    return f"{POWERMETRICS_PATH} {arguments}"


def permissions_script_path() -> Path:
    if getattr(sys, "frozen", False):
        #EcoMonitor.app/Contents/MacOS/EcoMonitor -> Contents/Resources
        return Path(sys.executable).resolve().parent.parent / "Resources" / PERMISSIONS_SCRIPT
    return Path(__file__).resolve().parents[3] / "scripts" / PERMISSIONS_SCRIPT


def permissions_command() -> str:
    return f"sudo bash {shlex.quote(str(permissions_script_path()))}"


def split_documents(buffer: bytes) -> tuple[list[bytes], bytes]:
    #powermetrics separa i documenti con un byte nullo
    documents: list[bytes] = []
    start = 0
    while True:
        end = buffer.find(_PLIST_END, start)
        if end < 0:
            break
        end += len(_PLIST_END)
        document = buffer[start:end].replace(b"\x00", b"").strip()
        if document:
            documents.append(document)
        start = end
    return documents, buffer[start:]


def _number(container: dict[str, Any], key: str) -> float | None:
    value = container.get(key)
    #bool e' un int per Python, ma non e' una misura
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _domain_watts(container: dict[str, Any], name: str, elapsed_seconds: float) -> float | None:
    #se manca la potenza si usa l'energia
    power_mw = _number(container, f"{name}_power")
    if power_mw is not None:
        return power_mw / 1000.0
    energy_mj = _number(container, f"{name}_energy")
    if energy_mj is not None:
        return energy_mj / 1000.0 / elapsed_seconds
    return None


def _checked(name: str, watts: float | None) -> float | None:
    if watts is None:
        return None
    if not math.isfinite(watts) or watts < 0.0 or watts > MAX_PLAUSIBLE_W:
        raise PowermetricsFormatError(f"{name} fuori scala: {watts!r} W")
    return watts


def parse_sample(document: bytes) -> PowermetricsSample:
    try:
        data = plistlib.loads(document.replace(b"\x00", b"").strip())
    except (plistlib.InvalidFileException, ExpatError, ValueError) as exc:
        raise PowermetricsFormatError(f"documento plist non leggibile: {exc}") from exc
    if not isinstance(data, dict):
        raise PowermetricsFormatError("il documento non e' un dizionario")

    elapsed_ns = _number(data, "elapsed_ns")
    if elapsed_ns is None or elapsed_ns <= 0:
        raise PowermetricsFormatError(
            f"manca elapsed_ns; chiavi presenti: {', '.join(sorted(data))}"
        )
    elapsed_seconds = elapsed_ns / 1e9

    processor = data.get("processor")
    if not isinstance(processor, dict):
        raise PowermetricsFormatError(
            f"manca 'processor'; chiavi presenti: {', '.join(sorted(data))}"
        )

    cpu_w = _checked("cpu", _domain_watts(processor, "cpu", elapsed_seconds))
    if cpu_w is None:
        #formato sconosciuto
        raise PowermetricsFormatError(
            "nessuna potenza CPU riconosciuta (serve Apple Silicon); chiavi di "
            f"'processor': {', '.join(sorted(processor))}"
        )

    gpu_w = _domain_watts(processor, "gpu", elapsed_seconds)
    gpu = data.get("gpu")
    if gpu_w is None and isinstance(gpu, dict):
        gpu_w = _domain_watts(gpu, "gpu", elapsed_seconds)

    return PowermetricsSample(
        elapsed_seconds=elapsed_seconds,
        cpu_w=cpu_w,
        gpu_w=_checked("gpu", gpu_w),
        ane_w=_checked("ane", _domain_watts(processor, "ane", elapsed_seconds)),
    )


@dataclass(frozen=True)
class AveragePower:
    cpu_w: float
    gpu_w: float | None
    ane_w: float | None


class EnergyAccumulator:
    #Energia e tempo dei campioni arrivati fra due take()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cpu_j = 0.0
        self._elapsed_s = 0.0
        self._gpu_j = 0.0
        self._gpu_elapsed_s = 0.0
        self._ane_j = 0.0
        self._ane_elapsed_s = 0.0
        self._last_sample: PowermetricsSample | None = None
        self._last_received_at: float | None = None

    def add(self, sample: PowermetricsSample, received_at: float) -> None:
        with self._lock:
            self._cpu_j += sample.cpu_w * sample.elapsed_seconds
            self._elapsed_s += sample.elapsed_seconds
            if sample.gpu_w is not None:
                self._gpu_j += sample.gpu_w * sample.elapsed_seconds
                self._gpu_elapsed_s += sample.elapsed_seconds
            if sample.ane_w is not None:
                self._ane_j += sample.ane_w * sample.elapsed_seconds
                self._ane_elapsed_s += sample.elapsed_seconds
            self._last_sample = sample
            self._last_received_at = received_at

    def take(self) -> AveragePower | None:
        with self._lock:
            if self._elapsed_s <= 0:
                return None
            average = AveragePower(
                cpu_w=self._cpu_j / self._elapsed_s,
                gpu_w=self._gpu_j / self._gpu_elapsed_s if self._gpu_elapsed_s > 0 else None,
                ane_w=self._ane_j / self._ane_elapsed_s if self._ane_elapsed_s > 0 else None,
            )
            self._cpu_j = self._elapsed_s = 0.0
            self._gpu_j = self._gpu_elapsed_s = 0.0
            self._ane_j = self._ane_elapsed_s = 0.0
            return average

    def last_sample(self) -> tuple[PowermetricsSample | None, float | None]:
        with self._lock:
            return self._last_sample, self._last_received_at


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _stop_process(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
    except OSError:
        pass


class MacosPowermetricsBackend:
    mode = NATIVE_MODE

    def __init__(
        self,
        popen: Callable[..., Any] = subprocess.Popen,
        running_as_root: bool | None = None,
        powermetrics_path: str = POWERMETRICS_PATH,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not Path(powermetrics_path).exists():
            raise SensorUnavailableError(f"{powermetrics_path} non esiste su questo sistema")

        self._popen = popen
        self._running_as_root = (
            _running_as_root() if running_as_root is None else running_as_root
        )
        self._powermetrics_path = powermetrics_path
        self._clock = clock
        self._accumulator = EnergyAccumulator()
        self._state_lock = threading.Lock()
        self._format_error: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)
        self._first_event = threading.Event()
        self._process: Any = None
        self._finalizer: weakref.finalize | None = None
        self._restarts_without_sample = 0

        self._start()
        self._first_event.wait(startup_timeout)

        sample, _ = self._accumulator.last_sample()
        if sample is None:
            reason = self._failure_reason(timeout=startup_timeout)
            self.close()
            raise SensorUnavailableError(reason)

        sample_cpu_percent()  # la prima chiamata vale sempre 0.0
        logger.info(
            "powermetrics attivo: CPU %.2f W, GPU %s, ogni %d ms",
            sample.cpu_w,
            f"{sample.gpu_w:.2f} W" if sample.gpu_w is not None else "n/d",
            SAMPLE_INTERVAL_MS,
        )

    def _start(self) -> None:
        command = powermetrics_command(self._running_as_root, self._powermetrics_path)
        try:
            process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise SensorUnavailableError(f"powermetrics non avviabile: {exc}") from exc

        self._process = process
        self._finalizer = weakref.finalize(self, _stop_process, process)
        threading.Thread(
            target=self._read_stdout, args=(process,), name="powermetrics-stdout", daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, args=(process,), name="powermetrics-stderr", daemon=True
        ).start()

    def _read_stdout(self, process: Any) -> None:
        stream = process.stdout
        buffer = b""
        try:
            while True:
                #read1 restituisce quello che c'e' senza aspettare 64 kB
                read = getattr(stream, "read1", None) or stream.read
                chunk = read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                buffer += chunk
                documents, buffer = split_documents(buffer)
                for document in documents:
                    self._consume(document)
                if len(buffer) > MAX_BUFFER_BYTES:
                    self._set_format_error(
                        f"oltre {MAX_BUFFER_BYTES} byte senza chiusura di un documento plist"
                    )
                    buffer = b""
        except (OSError, ValueError):
            #pipe chiusa da close()
            pass
        finally:
            self._first_event.set()

    def _consume(self, document: bytes) -> None:
        try:
            sample = parse_sample(document)
        except PowermetricsFormatError as exc:
            self._set_format_error(str(exc))
            return
        self._accumulator.add(sample, self._clock())
        self._first_event.set()

    def _set_format_error(self, message: str) -> None:
        with self._state_lock:
            first = self._format_error is None
            self._format_error = message
        if first:
            logger.warning("Campione di powermetrics scartato: %s", message)
        self._first_event.set()

    def _read_stderr(self, process: Any) -> None:
        try:
            for raw_line in process.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _failure_reason(self, timeout: float) -> str:
        process = self._process
        exit_code = process.poll() if process is not None else None
        if exit_code is not None:
            time.sleep(0.1)
        stderr = " | ".join(self._stderr_tail)
        with self._state_lock:
            format_error = self._format_error

        if format_error is not None:
            return f"formato di powermetrics non riconosciuto: {format_error}"
        if exit_code is not None:
            if not self._running_as_root and ("password" in stderr or "sudo" in stderr):
                return (
                    "powermetrics richiede root e manca la regola sudoers: eseguire una "
                    f"volta {permissions_command()} ({stderr})"
                )
            return f"powermetrics terminato con codice {exit_code}: {stderr or 'nessun messaggio'}"
        return f"nessun campione da powermetrics entro {timeout:g} s: {stderr or 'nessun messaggio'}"

    def read(self) -> PowerReading:
        average = self._accumulator.take()
        if average is None:
            average = self._recent_sample_or_raise()
        else:
            self._restarts_without_sample = 0

        return PowerReading(
            #il SoC meno la GPU: CPU e Neural Engine stanno sullo stesso package
            package_w=average.cpu_w + (average.ane_w or 0.0),
            #memoria unificata: nessun dominio DRAM separato
            dram_w=None,
            gpu_integrated_w=average.gpu_w,
            gpu_dedicated_w=None,
            timestamp=now_utc(),
            is_estimated=False,
            cpu_percent=sample_cpu_percent(),
        )

    def _recent_sample_or_raise(self) -> AveragePower:
        #nessun campione nuovo dall'ultima read()
        process = self._process
        if process is None or process.poll() is not None:
            reason = self._failure_reason(timeout=0)
            if self._restarts_without_sample >= MAX_RESTARTS_WITHOUT_SAMPLE:
                self.close()
                raise SensorLostError(
                    f"{reason}; {self._restarts_without_sample} riavvi di fila senza campioni"
                )
            self._restarts_without_sample += 1
            self._restart()
            raise SensorUnavailableError(f"{reason}; riavviato, la misura riprende al prossimo tick")

        sample, received_at = self._accumulator.last_sample()
        if sample is None or received_at is None:
            raise SensorUnavailableError("powermetrics non ha ancora prodotto campioni")
        age = self._clock() - received_at
        if age > STALE_AFTER_SECONDS:
            with self._state_lock:
                format_error = self._format_error
            detail = f" (ultimo errore: {format_error})" if format_error else ""
            raise SensorUnavailableError(
                f"nessun campione da powermetrics da {age:.0f} s{detail}"
            )
        return AveragePower(cpu_w=sample.cpu_w, gpu_w=sample.gpu_w, ane_w=sample.ane_w)

    def _restart(self) -> None:
        self.close()
        try:
            self._start()
        except SensorUnavailableError as exc:
            logger.warning("Riavvio di powermetrics non riuscito: %s", exc)

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None
        self._process = None
