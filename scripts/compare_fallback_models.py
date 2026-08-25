# Confronto dei modelli di stima contro la misura reale

# con 2 processi in parallelo: uno elevato che scrive nel database, 
# uno normale che legge e confronta

#     # terminale elevato
#     python -m ecomonitor.app

#     # terminale normale
#     python scripts/compare_fallback_models.py --seconds 120

# Confrontare due sessioni consecutive misurerebbe la differenza di carico fra
# l'una e l'altra, non la differenza fra i modelli

# I modelli sono valutati sullo stesso campione di utilizzo di CPU

import argparse
import ctypes
import math
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ecomonitor.sensors.base import SensorUnavailableError, sample_cpu_percent
from ecomonitor.sensors.calibration import cpu_calibration_from_database
from ecomonitor.sensors.fallback_model import DEFAULT_TDP_W
from ecomonitor.storage.db import LocalDatabase, default_database_path, to_utc_iso


def is_elevated() -> bool | None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    try:
        return bool(windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def measured_truth(path: Path, start: datetime, end: datetime) -> tuple[float, float, int]:
    #Le righe che il processo elevato ha scritto nella finestra
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT wh, cpu_wh, duration_seconds FROM energy_events "
            "WHERE is_estimated = 0 AND duration_seconds > 0 "
            "AND timestamp >= ? AND timestamp < ?",
            (to_utc_iso(start - timedelta(minutes=2)), to_utc_iso(end)),
        ).fetchall()

    if not rows:
        return 0.0, 0.0, 0

    total_w = sum(wh / (sec / 3600.0) for wh, _, sec in rows) / len(rows)
    with_cpu = [(c, s) for _, c, s in rows if c]
    cpu_w = (
        sum(c / (s / 3600.0) for c, s in with_cpu) / len(with_cpu) if with_cpu else 0.0
    )
    return total_w, cpu_w, len(rows)


def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita e non __doc__: l'intestazione di questo file e' fatta
    #di commenti, quindi __doc__ vale None e .split() solleverebbe AttributeError
    parser = argparse.ArgumentParser(
        description="Confronto dei modelli di stima contro la misura reale"
    )
    parser.add_argument("--seconds", type=float, default=120.0,
                        help="durata del campionamento (default 120)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="secondi fra un campione e l'altro (default 5)")
    parser.add_argument("--db", default=None, help="database da leggere")
    args = parser.parse_args(argv)

    path = Path(args.db) if args.db else default_database_path()
    elevated = is_elevated()

    print(f"database        : {path}")
    print(f"processo elevato: {elevated}")
    if elevated:
        print("\nATTENZIONE: elevato. Non usa i modelli di ripiego di")
        print("un utente senza privilegi, il confronto non misura quello che dovrebbe")
        return 1

    db = LocalDatabase(path)

    calibration = cpu_calibration_from_database(db)
    print("\n=== calibrazione del dominio CPU ===")
    if calibration is None:
        print("  non disponibile: servono righe misurate con cpu_percent e cpu_wh")
    else:
        print(f"  {calibration.describe()}")
        print(f"  modello    : {calibration.kind}")
        print(f"  escursione : {calibration.cpu_spread:.1f} punti percentuali")

    #Un solo backend aperto per le misurazioni dei vari modelli, sia gpu che cpu 
    try:
        from ecomonitor.sensors.windows_lhm import WindowsPartialLhmBackend

        backend = WindowsPartialLhmBackend(cpu_calibration=calibration)
    except (ImportError, SensorUnavailableError) as exc:
        print(f"\nBackend parziale non disponibile: {exc}")
        print("Non esiste un GPU dedicata leggibile")
        return 1

    print(f"\ncampionamento di {args.seconds:.0f} s ogni {args.interval:.0f} s...")
    start = datetime.now().astimezone()

    generic, partial, calibrated, cpus, gpus = [], [], [], [], []
    deadline = time.monotonic() + args.seconds
    sample_cpu_percent()  # la prima chiamata vale sempre 0.0
    while time.monotonic() < deadline:
        reading = backend.read()
        cpu = reading.cpu_percent or 0.0
        gpu = reading.gpu_dedicated_w or 0.0

        cpus.append(cpu)
        gpus.append(gpu)
        generic.append(DEFAULT_TDP_W * (cpu / 100.0))
        partial.append(gpu + DEFAULT_TDP_W * (cpu / 100.0))
        calibrated.append(gpu + calibration.predict(cpu) if calibration else float("nan"))
        time.sleep(args.interval)

    end = datetime.now().astimezone()
    backend.close()

    true_total, true_cpu, n_rows = measured_truth(path, start, end)

    def mean(values):
        return sum(values) / len(values) if values else 0.0

    print(f"\n=== condizioni ({len(cpus)} campioni) ===")
    print(f"  utilizzo CPU medio : {mean(cpus):.1f}%")
    print(f"  GPU misurata       : {mean(gpus):.1f} W")

    if n_rows == 0:
        print("\nNessuna riga misurata nella finestra")
        return 1

    print(f"\n=== verita' ({n_rows} righe misurate) ===")
    print(f"  totale             : {true_total:.1f} W")
    print(f"  di cui CPU         : {true_cpu:.1f} W")

    print("\n=== confronto ===")
    print(f"{'livello':<34} {'stima':>9} {'errore':>9}")
    print("-" * 55)
    for label, values in (
        ("ripiego generico", generic),
        ("backend parziale", partial),
        ("CPU calibrata", calibrated),
    ):
        value = mean(values)
        if math.isnan(value):  # calibrazione assente
            print(f"{label:<34} {'n/d':>9} {'n/d':>9}")
            continue
        error = (value - true_total) / true_total * 100.0
        print(f"{label:<34} {value:>7.1f} W {error:>+8.0f}%")

    if calibration is not None and true_cpu > 0:
        cpu_est = calibration.predict(mean(cpus))
        print(f"\n  termine CPU stimato: {cpu_est:.1f} W contro {true_cpu:.1f} W veri "
              f"({(cpu_est - true_cpu) / true_cpu * 100:+.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
