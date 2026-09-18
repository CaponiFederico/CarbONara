#Verifica il backend powermetrics su Mac Silicon
#
#   python scripts/diagnose_macos_sensors.py
#   python scripts/diagnose_macos_sensors.py --output powermetrics_raw.plist --load

import argparse
import os
import platform
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path

from ecomonitor.sensors.base import SensorUnavailableError
from ecomonitor.sensors.macos_powermetrics import (
    POWERMETRICS_PATH,
    MacosPowermetricsBackend,
    PowermetricsFormatError,
    parse_sample,
    permissions_command,
    powermetrics_command,
    split_documents,
)

OK = "[ OK ]"
WARN = "[WARN]"
FAIL = "[FAIL]"
INFO = "[INFO]"

ENERGY_POWER_TOLERANCE = 0.10

LOAD_RATIO_MIN = 1.5

_failures = 0


def _line(marker: str, text: str) -> None:
    global _failures
    if marker == FAIL:
        _failures += 1
    print(f"{marker} {text}")


def _title(text: str) -> None:
    print(f"\n=== {text}")

def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def check_environment() -> None:
    _title("Ambiente")
    release, _, machine = platform.mac_ver()
    print(f"{INFO} macOS        : {release or 'n/d'} ({machine or platform.machine()})")
    print(f"{INFO} python       : {sys.version.split()[0]} {platform.machine()} ({sys.executable})")
    try:
        translated = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"], 
            #parametri per sysctl.proc_translated: 
            #0=arm64, 1=rosetta, non esiste=arm64
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        translated = ""
    if translated == "1":
        _line(WARN, "python gira sotto Rosetta: usare un interprete arm64")
    try:
        model = subprocess.run(
            ["sysctl", "-n", "hw.model"], 
            capture_output=True, 
            text=True, 
            check=False, 
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        model = ""
    print(f"{INFO} modello      : {model or 'n/d'}")


def check_privileges() -> bool:
    _title("Privilegi")
    if not Path(POWERMETRICS_PATH).exists():
        _line(FAIL, f"{POWERMETRICS_PATH} non esiste")
        return False
    if _is_root():
        _line(OK, "processo root: powermetrics si lancia senza sudo")
        return True
    command = powermetrics_command(running_as_root=False)
    #sudo -l <comando> dice se quella riga esatta e' concessa, senza eseguirla
    listed = subprocess.run(
        [command[0], "-n", "-l", *command[2:]],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if listed.returncode == 0:
        _line(OK, "sudo -n concede la riga di comando dell'app")
        return True
    _line(FAIL, f"sudo -n rifiuta powermetrics: eseguire {permissions_command()}")
    print(f"{INFO} sudo dice: {(listed.stderr or listed.stdout).strip() or 'niente'}")
    return False


def capture_raw_stream(seconds: float, output: Path) -> bytes:
    _title(f"Flusso grezzo di powermetrics per {seconds:g} s")
    command = powermetrics_command(_is_root())
    print(f"{INFO} comando: {' '.join(command)}")
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    chunks: list[bytes] = [] 

    stream = process.stdout
    assert stream is not None

    def reader() -> None:
        chunks.extend(iter(lambda: stream.read1(65536), b"")) 

    thread = threading.Thread(target=reader, daemon=True) 
    thread.start()
    time.sleep(seconds) 
    process.terminate()
    try:
        _, stderr = process.communicate(timeout=5) 
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate()
    thread.join(timeout=5) 

    raw = b"".join(chunks) 
    output.write_bytes(raw)
    print(f"{INFO} salvati {len(raw)} byte in {output.resolve()}")
    if stderr.strip():
        print(f"{INFO} stderr: {stderr.decode('utf-8', errors='replace').strip()}")
    if not raw:
        _line(FAIL, f"nessun byte da powermetrics (codice {process.returncode})")
    return raw


def inspect_documents(raw: bytes) -> None:
    documents, rest = split_documents(raw)
    print(f"{INFO} documenti completi: {len(documents)}, byte residui: {len(rest)}")
    if not documents:
        _line(FAIL, "nessun documento plist completo: separatore o formato diversi dall'atteso")
        return

    try:
        first = plistlib.loads(documents[0])
    except Exception as exc:
        _line(FAIL, f"il primo documento non e' un plist leggibile: {exc}")
        return
    if isinstance(first, dict):
        print(f"{INFO} chiavi del documento : {', '.join(sorted(first))}")
        for section in ("processor", "gpu"):
            content = first.get(section)
            if isinstance(content, dict):
                print(f"{INFO} chiavi di '{section}'{' ' * (9 - len(section))}: "
                      f"{', '.join(sorted(content))}")

    parsed = 0
    for index, document in enumerate(documents, start=1):
        try:
            sample = parse_sample(document)
        except PowermetricsFormatError as exc:
            _line(FAIL, f"documento {index} scartato: {exc}")
            continue
        parsed += 1
        gpu = f"{sample.gpu_w:.3f} W" if sample.gpu_w is not None else "n/d"
        ane = f"{sample.ane_w:.3f} W" if sample.ane_w is not None else "n/d"
        print(
            f"{INFO} documento {index}: {sample.elapsed_seconds:.3f} s, "
            f"CPU {sample.cpu_w:.3f} W, GPU {gpu}, ANE {ane}"
        )
        _compare_energy_and_power(document, index)
    if parsed:
        _line(OK, f"{parsed} documenti su {len(documents)} letti dal parser dell'app")


def _compare_energy_and_power(document: bytes, index: int) -> None:
    # confronta i valori di potenza e energia per CPU, GPU e ANE
    data = plistlib.loads(document)
    processor = data.get("processor", {})
    elapsed_s = data.get("elapsed_ns", 0) / 1e9
    for name in ("cpu", "gpu", "ane"):
        power_mw = processor.get(f"{name}_power")
        energy_mj = processor.get(f"{name}_energy")
        if not isinstance(power_mw, int | float) or not isinstance(energy_mj, int | float):
            continue
        if elapsed_s <= 0:
            continue
        from_power = power_mw / 1000.0
        from_energy = energy_mj / 1000.0 / elapsed_s
        reference = max(from_power, from_energy)
        if reference < 0.05:
            continue
        gap = abs(from_power - from_energy) / reference
        marker = OK if gap <= ENERGY_POWER_TOLERANCE else WARN
        _line(
            marker,
            f"documento {index} {name}: {from_power:.3f} W da _power, "
            f"{from_energy:.3f} W da _energy/elapsed (scarto {gap:.0%})",
        )


def check_backend(samples: int, interval: float, load: bool) -> None:
    _title(f"Backend dell'app: {samples} letture a {interval:g} s")
    try:
        backend = MacosPowermetricsBackend()
    except SensorUnavailableError as exc:
        _line(FAIL, f"backend non disponibile: {exc}")
        return

    try:
        backend.read()
        idle_cpu: list[float] = []
        for index in range(samples):
            time.sleep(interval)
            try:
                reading = backend.read()
            except SensorUnavailableError as exc:
                _line(FAIL, f"lettura {index + 1} fallita: {exc}")
                continue
            idle_cpu.append(reading.package_w)
            gpu = f"{reading.gpu_integrated_w:.3f} W" if reading.gpu_integrated_w is not None else "n/d"
            print(
                f"{INFO} {index + 1:>2}: CPU+ANE {reading.package_w:.3f} W, GPU {gpu}, "
                f"totale {reading.total_w:.3f} W, CPU {reading.cpu_percent or 0:.0f}%"
            )
        if idle_cpu and all(value > 0 for value in idle_cpu):
            _line(OK, "potenza CPU sempre positiva")
        elif idle_cpu:
            _line(FAIL, "almeno una lettura a 0 W: sensore non valido")

        if load and idle_cpu:
            _check_load_response(backend, sum(idle_cpu) / len(idle_cpu))
    finally:
        backend.close()


def _check_load_response(backend: MacosPowermetricsBackend, idle_w: float) -> None:
    workers = max(1, (os.cpu_count() or 2) // 2)
    print(f"{INFO} carico: {workers} processi a ciclo stretto per 5 s")
    spinners = [
        subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(workers)
    ]
    try:
        time.sleep(1)
        backend.read()  # scarta il secondo di avvio dei processi
        time.sleep(4)
        loaded = backend.read()
    finally:
        for spinner in spinners:
            spinner.kill()
    ratio = loaded.package_w / idle_w if idle_w > 0 else float("inf")
    marker = OK if ratio >= LOAD_RATIO_MIN else WARN
    _line(
        marker,
        f"sotto carico {loaded.package_w:.3f} W contro {idle_w:.3f} W a riposo "
        f"({ratio:.1f}x, atteso almeno {LOAD_RATIO_MIN:g}x)",
    )


def check_factory() -> None:
    _title("Scelta del backend")
    from ecomonitor.sensors.factory import get_backend

    backend = get_backend()
    name = type(backend).__name__
    mode = getattr(backend, "mode", None)
    print(f"backend sensori: {name}")
    if name == "MacosPowermetricsBackend":
        _line(OK, "la factory sceglie la misura nativa")
    else:
        advice = getattr(mode, "advice", None)
        _line(FAIL, f"la factory ripiega su {name} ({getattr(mode, 'kind', '?')}): {advice}")
    close = getattr(backend, "close", None)
    if close is not None:
        close()


def check_battery() -> None:
    _title("Batteria")
    import psutil

    battery = psutil.sensors_battery()
    if battery is None:
        _line(INFO, "nessuna batteria: scenario DESKTOP, attribuzione sempre da rete")
    else:
        state = "collegato" if battery.power_plugged else "a batteria"
        _line(INFO, f"batteria {battery.percent:.0f}% ({state}): scenario LAPTOP")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verifica di powermetrics e del backend macOS.")
    parser.add_argument("--output", default="powermetrics_raw.plist", help="flusso grezzo salvato")
    parser.add_argument("--raw-seconds", type=float, default=3.5) 
    parser.add_argument("--samples", type=int, default=5) 
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--load", action="store_true", help="verifica la risposta al carico")
    args = parser.parse_args(argv)

    if platform.system() != "Darwin":
        print(f"{FAIL} va eseguito su macOS, non su {platform.system()}")
        return 1

    check_environment()
    if check_privileges():
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        inspect_documents(capture_raw_stream(args.raw_seconds, output))
        check_backend(max(1, args.samples), max(1.0, args.interval), args.load)
    check_factory()
    check_battery()

    print()
    if _failures:
        print(f"ESITO: {_failures} controllo/i FALLITO/I")
        return 1
    print("ESITO: nessun fallimento")
    return 0


if __name__ == "__main__":
    sys.exit(main())
