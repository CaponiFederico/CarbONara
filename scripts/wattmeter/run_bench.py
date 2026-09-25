# Campagna di validazione 
#
#     python scripts/wattmeter/run_bench.py --keys measurements/keys.json --id 1002b28f41


import argparse
import multiprocessing
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

ROOT = Path(__file__).resolve().parents[2]

#Ordine dei gradini di carico
STEP_ORDERS = (
    (0.25, 0.5, 1.0),
    (0.5, 1.0, 0.25),
    (1.0, 0.25, 0.5),
)

def burn(deadline: float) -> None:
    #ciclo vuoto
    if psutil is not None:
        try:
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except (psutil.Error, OSError, AttributeError):
            pass
    while time.monotonic() < deadline:
        pass


def _say(text: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


def is_elevated() -> bool | None:
    #None quando non si puo' sapere
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return None


def run_load(workers: int, minutes: float) -> None:
    deadline = time.monotonic() + minutes * 60.0
    processes = [
        multiprocessing.Process(target=burn, args=(deadline,)) for _ in range(workers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()


def run_session(name: str, order: tuple[float, ...], args: argparse.Namespace,
                threads: int) -> dict[str, Path]:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    database = output / f"{name}.db"
    plug_csv = output / f"{name}_plug.csv"

    #i due registratori si fermano da soli
    total = args.rest_minutes + args.step_minutes * len(order) + 1.0

    _say(f"{name}: avvio, durata prevista {total:.0f} min")

    monitor = subprocess.Popen(
        [sys.executable, "-m", "ecomonitor.cli", "--interval", str(args.interval),
         "--minutes", str(total), "--db", str(database), "--quiet"],
        cwd=ROOT,
    )
    recorder = subprocess.Popen(
        [sys.executable, *recorder_arguments(args),
         "--minutes", str(total), "--output", str(plug_csv)],
        cwd=ROOT,
    )

    try:
        time.sleep(15)

        _say(f"{name}: riposo per {args.rest_minutes:.0f} min")
        time.sleep(args.rest_minutes * 60.0)

        for fraction in order:
            workers = max(1, round(threads * fraction))
            _say(f"{name}: carico {fraction:.0%} ({workers} processi) "
                 f"per {args.step_minutes:.0f} min")
            run_load(workers, args.step_minutes)

        _say(f"{name}: gradini finiti, attendo la chiusura dei registratori")
        monitor.wait()
        recorder.wait()
    except KeyboardInterrupt:
        _say("interrotto: fermo i processi della sessione")
        for process in (monitor, recorder):
            process.terminate()
        raise

    _say(f"{name}: chiusa")
    return {"db": database, "plug": plug_csv}


def recorder_arguments(args: argparse.Namespace) -> list[str]:
    #lo script che registra la presa, con le sole opzioni che la identificano
    if args.plug == "sem6000":
        return [str(Path(__file__).with_name("read_sem6000.py")),
                "--address", args.address, "--pin", args.pin]
    return [str(Path(__file__).with_name("read_plug.py")),
            "--keys", args.keys, "--id", args.id]


def compare(name: str, files: dict[str, Path], output_dir: str) -> None:
    pairs = Path(output_dir) / f"{name}_pairs.csv"
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("compare_plug.py")),
         "--plug", str(files["plug"]), "--db", str(files["db"]),
         "--pairs", str(pairs)],
        cwd=ROOT,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita
    parser = argparse.ArgumentParser(
        description="Esegue l'intera campagna di validazione senza interazione"
    )
    parser.add_argument("--plug", choices=("sonoff", "sem6000"), default="sonoff",
                        help="quale presa registra la potenza")
    parser.add_argument("--keys", default="measurements/keys.json",
                        help="sonoff: JSON id->devicekey")
    parser.add_argument("--id", default=None, help="sonoff: id del dispositivo da registrare")
    parser.add_argument("--address", default=None, help="sem6000: indirizzo Bluetooth")
    parser.add_argument("--pin", default="0000", help="sem6000: PIN della presa")
    parser.add_argument("--sessions", type=int, default=3, help="quante ripetizioni")
    parser.add_argument("--step-minutes", type=float, default=10.0)
    parser.add_argument("--rest-minutes", type=float, default=10.0)
    parser.add_argument("--cooldown-minutes", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--output-dir", default="measurements")
    parser.add_argument("--prefix", default="session")
    parser.add_argument(
        "--skip-elevation-check",
        action="store_true",
        help="prosegue anche senza privilegi: la CPU sara' stimata, non misurata",
    )
    args = parser.parse_args(argv)

    if args.sessions < 1:
        parser.error("--sessions deve essere almeno 1")
    if args.plug == "sem6000":
        if not args.address:
            parser.error("con --plug sem6000 serve --address (read_sem6000.py --list)")
    else:
        if not args.id:
            parser.error("con --plug sonoff serve --id")
        if not Path(args.keys).exists():
            parser.error(f"{args.keys} non esiste: eseguire prima device_key.py")

    elevated = is_elevated()
    if elevated is False and not args.skip_elevation_check:
        print(
            "Questo terminale non e' elevato. Senza privilegi LibreHardwareMonitor\n"
            "non legge gli MSR e la CPU viene stimata\n"
            "ore producendo un confronto non interpretabile.\n",
            file=sys.stderr,
        )
        return 2

    #read_plug.py apre il CSV in append e la CLI riapre il database esistente
    existing = [
        path
        for index in range(args.sessions)
        for path in (
            Path(args.output_dir) / f"{args.prefix}{index + 1:02d}.db",
            Path(args.output_dir) / f"{args.prefix}{index + 1:02d}_plug.csv",
        )
        if path.exists()
    ]
    if existing:
        print(
            "Questi file esistono gia' e verrebbero riempiti insieme ai dati nuovi:\n  "
            + "\n  ".join(str(path) for path in existing)
            + "\nUsa --prefix per una campagna nuova, o sposta quelli vecchi.",
            file=sys.stderr,
        )
        return 2

    threads = multiprocessing.cpu_count()
    session_minutes = args.rest_minutes + args.step_minutes * 3 + 1.0
    total = session_minutes * args.sessions + args.cooldown_minutes * (args.sessions - 1)
    _say(f"{args.sessions} sessioni su {threads} thread logici, "
         f"circa {total:.0f} min in tutto. Non toccare questa finestra.")

    produced: dict[str, dict[str, Path]] = {}
    for index in range(args.sessions):
        name = f"{args.prefix}{index + 1:02d}"
        order = STEP_ORDERS[index % len(STEP_ORDERS)]
        produced[name] = run_session(name, order, args, threads)

        if index < args.sessions - 1:
            _say(f"raffreddamento {args.cooldown_minutes:.0f} min")
            time.sleep(args.cooldown_minutes * 60.0)

    #flush esplicito
    print("\n" + "=" * 70, flush=True)
    for name, files in produced.items():
        print(f"\n----- {name} -----", flush=True)
        compare(name, files, args.output_dir)

    print("\nCampagna finita. I coefficienti delle sessioni vanno confrontati fra "
          "loro: se a e b si somigliano, la deriva termica non li sta guidando.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
