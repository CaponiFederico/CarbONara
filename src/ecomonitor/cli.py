import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from ecomonitor.orchestrator import Orchestrator
from ecomonitor.storage.db import LocalDatabase, default_database_path
from ecomonitor.storage.lock import DatabaseLock

#Prototipo da riga di comando
logger = logging.getLogger(__name__)

#serve a testare il backend e l'orchestrator senza GUI, e a raccogliere dati
_GRID_SOURCE_LABELS = {
    "live": "dati live",
    "static_fallback": "media statica",
}


def _intensity_source(result: dict) -> str:
    attribution = result["attribution"]
    source = attribution.source.value

    if source == "grid_live":
        grid_label = _GRID_SOURCE_LABELS.get(
            result["grid_intensity_source"], result["grid_intensity_source"]
        )
        return f"da rete ({grid_label})"
    if source == "battery_pool":
        return "da storico batteria"
    if source == "battery_pool_partial":
        return f"da storico parziale ({attribution.pool_coverage_ratio * 100:.0f}% tracciato)"
    if source == "battery_pool_fallback":
        reference_label = (
            "media nazionale"
            if result["unknown_origin_source"] == "static_country_mean"
            else "media finestra scoperta"
        )
        return f"senza storico ({reference_label})"
    return source


def _format_tick(result: dict) -> str:
    reading = result["reading"]
    attribution = result["attribution"]
    state = result["power_state"]

    power_source = "rete" if state.is_plugged_in else "batteria"
    if state.battery_percent is not None:
        power_source += f" {state.battery_percent:.0f}%"

    estimated_marker = "~" if reading.is_estimated else " "
    return (
        f"{datetime.now():%H:%M:%S}  {estimated_marker}{reading.total_w:6.2f} W  "
        f"oggi {result['today_energy_wh']:7.2f} Wh  {result['today_operational_gco2']:7.2f} gCO2  "
        f"{attribution.intensity_g_per_kwh:4.0f} gCO2/kWh {_intensity_source(result)}  "
        f"[{power_source}]"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ecomonitor-cli",
        description="Monitoraggio di energia e CO2eq da riga di comando.",
    )
    parser.add_argument("--interval", type=float, default=None, help="secondi fra i tick")
    parser.add_argument("--minutes", type=float, default=None, help="durata, altrimenti fino a Ctrl+C")
    parser.add_argument("--db", default=None, help="usa questo database invece di quello utente")
    parser.add_argument("--quiet", action="store_true", help="stampa solo il riepilogo finale")
    args = parser.parse_args(argv)

    if args.interval is not None and args.interval <= 0:
        parser.error("--interval deve essere positivo")
    if args.minutes is not None and args.minutes < 0:
        parser.error("--minutes non puo' essere negativo")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    #Il lock va preso prima di costruire l'Orchestrator che apre il database
    db_path = Path(args.db) if args.db else default_database_path()
    lock = DatabaseLock(db_path)
    if not lock.acquire():
        parser.error(
            f"il database {db_path} e' gia' in uso da un'altra istanza di EcoMonitor "
            "(l'applicazione grafica, o un'altra esecuzione di questo comando). "
            "Chiudila, oppure usa --db per lavorare su un file separato"
        )

    try:
        return _run(args, parser, db_path)
    finally:
        lock.release()


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser, db_path: Path) -> int:
    try:
        orchestrator = Orchestrator(db=LocalDatabase(db_path))
    except OSError as exc:
        parser.error(f"impossibile aprire il database ({exc})")
    config = orchestrator.config_manager.config
    interval = args.interval if args.interval is not None else config.poll_interval_seconds

    print(f"backend sensori : {type(orchestrator.backend).__name__}")
    print(f"zona rete       : {orchestrator.grid_service.zone} ({config.country_code})")
    print(f"modello pool    : {orchestrator.battery_pool.model.value}")
    print(f"intervallo      : {interval:g}s — Ctrl+C per uscire\n")

    deadline = time.monotonic() + args.minutes * 60 if args.minutes is not None else None
    ticks = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            result = orchestrator.tick()
            ticks += 1
            if not args.quiet:
                print(_format_tick(result))
                #il primo tick non ha un intervallo precedente, quindi vale
                #sempre 0 Wh: e' atteso, non un errore
                if ticks == 1:
                    print("  (il primo tick non ha ancora un intervallo da integrare)")
                print(f"  ricarica: {result['advice'].message()}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
    finally:
        orchestrator.flush()

    report = orchestrator.build_daily_report()
    print(f"\ntick eseguiti   : {ticks}")
    print(f"oggi            : {report.energy_wh:.2f} Wh, {report.operational_gco2:.2f} gCO2eq")
    print(f"ore attive      : {report.active_hours:.2f} h")
    print(f"equivalente     : {report.equivalent_car_km:.3f} km in auto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
