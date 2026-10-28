# Quanto consuma CarbO'Nara stesso, misurato alla presa
#
# Una copia sola sta sotto il rumore della presa: se ne lanciano N nei bracci B,
# alternati a bracci A senza copie, e la differenza si divide per N.
#
#     python scripts/wattmeter/measure_self.py --address <indirizzo> --copies 20,40

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[2]

PRIVATE_FIELDS = ("electricitymaps_api_key", "study_token", "study_endpoint")

MIN_SIGNAL_TO_ERROR = 2.0
MAX_LEVEL_SPREAD = 0.2
#watt di scarto fra i due A attorno a un B oltre i quali il B si scarta
MAX_BASELINE_STEP_W = 1.0


def _say(text: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


def run_instance(db_path: Path, config_path: Path, offset: float) -> int:
    #il ciclo della CLI
    time.sleep(offset)
    from ecomonitor.config import ConfigManager
    from ecomonitor.orchestrator import Orchestrator
    from ecomonitor.storage.db import LocalDatabase
    from ecomonitor.storage.lock import DatabaseLock

    lock = DatabaseLock(db_path)
    if not lock.acquire():
        return 2
    orchestrator = Orchestrator(config_manager=ConfigManager(config_path), db=LocalDatabase(db_path))
    interval = orchestrator.config_manager.config.poll_interval_seconds
    #quale backend ha davvero preso la copia: misurato o stimato cambia il costo
    db_path.with_suffix(".backend").write_text(type(orchestrator.backend).__name__, encoding="utf-8")
    while True:
        try:
            orchestrator.tick()
        except Exception as error:
            print(error, file=sys.stderr)
        time.sleep(interval)


def write_instance_config(path: Path) -> dict:
    from ecomonitor.config import ConfigManager

    values = asdict(ConfigManager().config)
    for name in PRIVATE_FIELDS:
        values[name] = None
    path.write_text(json.dumps(values), encoding="utf-8")
    return values


def tree(process: subprocess.Popen) -> list[psutil.Process]:
    try:
        parent = psutil.Process(process.pid)
        return [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return []


def cpu_seconds(processes: list[subprocess.Popen]) -> float:
    total = 0.0
    for process in processes:
        for item in tree(process):
            try:
                times = item.cpu_times()
            except psutil.Error:
                continue
            total += times.user + times.system
    return total


def memory_mb(processes: list[subprocess.Popen]) -> float:
    total = 0.0
    for process in processes:
        for item in tree(process):
            try:
                total += item.memory_info().rss
            except psutil.Error:
                continue
    return total / 1e6


def start_instances(count: int, folder: Path, config_path: Path, tag: str,
                    spread: float) -> list[subprocess.Popen]:
    #partenze distribuite su un intervallo di scrittura
    return [
        subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--instance",
             "--db", str(folder / f"{tag}_{index:03d}.db"), "--config", str(config_path),
             "--offset", str(spread * index / count)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for index in range(count)
    ]


def stop_instances(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        for item in reversed(tree(process)):
            try:
                item.kill()
            except psutil.Error:
                pass
    for process in processes:
        process.wait()


def load_plug(path: Path) -> list[tuple[float, float]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            (datetime.fromisoformat(row["timestamp_utc"]).timestamp(), float(row["power_w"]))
            for row in csv.DictReader(f)
        ]


def analyze(arms: list[dict], samples: list[tuple[float, float]]) -> int:
    for arm in arms:
        values = [power for moment, power in samples if arm["from"] <= moment < arm["to"]]
        arm["n"] = len(values)
        arm["mean"] = statistics.fmean(values) if values else None
        arm["median"] = statistics.median(values) if values else None

    print("\nbraccio  copie  campioni   media W  mediana W  cpu per copia")
    for arm in arms:
        if arm["mean"] is None:
            print(f"  {arm['index']:>3}    {arm['copies']:>4}        0   nessun campione")
            continue
        cpu = f"{arm['cpu_percent_per_copy']:.3f}% di un core" if arm["copies"] else ""
        print(f"  {arm['index']:>3}    {arm['copies']:>4}   {arm['n']:>6}   {arm['mean']:7.2f}   "
              f"{arm['median']:8.2f}  {cpu}")

    #ogni braccio B contro la media dei due A che lo racchiudono
    deltas: dict[int, dict[str, list[float]]] = {}
    for position, arm in enumerate(arms):
        if not arm["copies"]:
            continue
        before, after = arms[position - 1], arms[position + 1]
        if None in (arm["mean"], before["mean"], after["mean"]):
            continue
        #se la base cambia stato fra i due A, la loro media non e' la base di nessuno
        step = abs(before["median"] - after["median"])
        if step > MAX_BASELINE_STEP_W:
            print(f"braccio {arm['index']} scartato: la base attorno si sposta di {step:.1f} W")
            continue
        level = deltas.setdefault(arm["copies"], {"mean": [], "median": []})
        for key in ("mean", "median"):
            level[key].append(arm[key] - (before[key] + after[key]) / 2.0)

    per_copy: dict[int, float] = {}
    print()
    for copies, level in sorted(deltas.items()):
        for key in ("mean", "median"):
            values = level[key]
            delta = statistics.fmean(values)
            listed = ", ".join(f"{value:+.2f}" for value in values)
            if len(values) < 2:
                print(f"{copies} copie, {key}: {delta:+.2f} W ({listed}) — una coppia sola, "
                      "nessuna incertezza: non e' una misura")
                continue
            error = statistics.stdev(values) / len(values) ** 0.5
            line = f"{copies} copie, {key}: {delta:+.2f} ± {error:.2f} W ({listed})"
            if delta < MIN_SIGNAL_TO_ERROR * error:
                bound = max(delta, 0.0) + MIN_SIGNAL_TO_ERROR * error
                print(f"{line} — non rilevabile; per copia sotto {bound / copies * 1000:.0f} mW")
                continue
            print(f"{line} — per copia {delta / copies * 1000:.0f} ± {error / copies * 1000:.0f} mW")
            if key == "mean":
                per_copy[copies] = delta / copies

    if len(per_copy) < 2:
        print("\nLinearita' non verificabile: serve un consumo rilevato ad almeno due livelli.")
        return 1
    low, high = min(per_copy.values()), max(per_copy.values())
    spread = (high - low) / high
    if spread > MAX_LEVEL_SPREAD:
        print(f"\nI consumi per copia ai vari livelli differiscono del {spread:.0%}: la differenza "
              "non e' proporzionale a N, dividere non e' giustificato.")
        return 1
    print(f"\nConsumi per copia coerenti fra i livelli (scarto {spread:.0%}): dividere per N regge.")
    return 0


def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita
    parser = argparse.ArgumentParser(
        description="Misura alla presa il consumo di CarbO'Nara lanciandone N copie a bracci alterni"
    )
    parser.add_argument("--instance", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--db", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--offset", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--address", default=None, help="indirizzo Bluetooth della SEM6000")
    parser.add_argument("--pin", default="0000")
    parser.add_argument("--copies", default="20,40",
                        help="copie nei bracci B, uno o piu' livelli separati da virgola")
    parser.add_argument("--pairs", type=int, default=3, help="bracci B per ogni livello")
    parser.add_argument("--arm-minutes", type=float, default=4.5)
    parser.add_argument("--settle-seconds", type=float, default=120.0,
                        help="secondi scartati all'inizio di ogni braccio")
    parser.add_argument("--output-dir", default="measurements")
    parser.add_argument("--prefix", default="self")
    args = parser.parse_args(argv)

    if args.instance:
        return run_instance(Path(args.db), Path(args.config), args.offset)

    if not args.address:
        parser.error("serve --address (read_sem6000.py --list)")
    levels = [int(text) for text in args.copies.split(",")]
    if min(levels) < 1 or args.pairs < 1:
        parser.error("--copies e --pairs devono essere almeno 1")
    if args.settle_seconds >= args.arm_minutes * 60.0:
        parser.error("--settle-seconds deve stare dentro il braccio")

    output = Path(args.output_dir)
    work = output / f"{args.prefix}_instances"
    plug_csv = output / f"{args.prefix}_plug.csv"
    arms_csv = output / f"{args.prefix}_arms.csv"
    if plug_csv.exists() or arms_csv.exists():
        print(f"{plug_csv} o {arms_csv} esistono gia': usa --prefix.", file=sys.stderr)
        return 2
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.json"
    spread = float(write_instance_config(config_path)["db_flush_interval_seconds"])
    if args.settle_seconds < spread + 30.0:
        parser.error(f"--settle-seconds deve coprire le partenze distribuite: almeno {spread + 30:.0f}")

    #A B A B ... A, coi livelli a rotazione
    sequence = [0]
    for _ in range(args.pairs):
        for level in levels:
            sequence += [level, 0]
    total_minutes = len(sequence) * args.arm_minutes
    _say(f"{len(sequence)} bracci da {args.arm_minutes:g} min, circa {total_minutes:.0f} min. "
         "Non usare la macchina.")

    recorder = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("read_sem6000.py")),
         "--address", args.address, "--pin", args.pin,
         "--minutes", str(total_minutes + 1.0), "--output", str(plug_csv)],
        cwd=ROOT, stdout=subprocess.DEVNULL,
    )
    time.sleep(20)

    arms: list[dict] = []
    backends: set[str] = set()
    running: list[subprocess.Popen] = []
    try:
        for index, copies in enumerate(sequence, start=1):
            _say(f"braccio {index}/{len(sequence)}: {copies} copie")
            start = time.time()
            running = (
                start_instances(copies, work, config_path, f"arm{index:02d}", spread)
                if copies else []
            )
            time.sleep(args.settle_seconds)
            measured_from = time.time()
            cpu_before = cpu_seconds(running)
            time.sleep(args.arm_minutes * 60.0 - args.settle_seconds)
            measured_to = time.time()
            cpu_used = cpu_seconds(running) - cpu_before
            alive = sum(1 for process in running if process.poll() is None)
            memory = memory_mb(running)
            stop_instances(running)
            running = []
            if alive != copies:
                _say(f"  ATTENZIONE: vive {alive} copie su {copies}")
            if copies:
                names = [path.read_text(encoding="utf-8")
                         for path in work.glob(f"arm{index:02d}_*.backend")]
                backends.update(names)
                _say(f"  backend: {len(names)} dichiarati, {sorted(set(names))}")
            arms.append({
                "index": index, "copies": copies, "alive": alive, "start": start,
                "from": measured_from, "to": measured_to,
                "cpu_percent_per_copy":
                    cpu_used / (measured_to - measured_from) / copies * 100.0 if copies else 0.0,
                "memory_mb_per_copy": memory / copies if copies else 0.0,
            })
    finally:
        stop_instances(running)
        recorder.terminate()
        recorder.wait()

    with arms_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["arm", "copies", "alive", "measured_from_utc", "measured_to_utc",
                         "cpu_percent_per_copy", "memory_mb_per_copy"])
        for arm in arms:
            writer.writerow([
                arm["index"], arm["copies"], arm["alive"],
                datetime.fromtimestamp(arm["from"], UTC).isoformat(),
                datetime.fromtimestamp(arm["to"], UTC).isoformat(),
                round(arm["cpu_percent_per_copy"], 4), round(arm["memory_mb_per_copy"], 1),
            ])

    print(f"\nbackend delle copie: {', '.join(sorted(backends)) or 'nessuno dichiarato'}")
    return analyze(arms, load_plug(plug_csv))


if __name__ == "__main__":
    sys.exit(main())
