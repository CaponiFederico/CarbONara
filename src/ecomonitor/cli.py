import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from ecomonitor.labels import grid_source_detail, short_source_label
from ecomonitor.orchestrator import Orchestrator
from ecomonitor.reporting import format_energy, format_gco2, format_phone_charges
from ecomonitor.storage.db import LocalDatabase, default_database_path
from ecomonitor.storage.lock import DatabaseLock

#Prototipo da riga di comando
logger = logging.getLogger(__name__)


def _intensity_source(result: dict) -> str:
    #le stesse etichette della finestra, da ecomonitor.labels
    attribution = result["attribution"]
    source = attribution.source.value
    label = short_source_label(source)

    if source == "grid_live":
        detail = grid_source_detail(
            result["grid_intensity_source"], result["grid_intensity_failure"]
        )
    elif source == "battery_pool_partial":
        detail = f"{attribution.pool_coverage_ratio * 100:.0f}% tracciato"
    elif source == "battery_pool_fallback":
        detail = (
            "media nazionale"
            if result["unknown_origin_source"] == "static_country_mean"
            else "media finestra scoperta"
        )
    else:
        return label
    return f"{label} ({detail})"


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
            f"il database {db_path} e' gia' in uso da un'altra istanza di CarbO'Nara "
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
    ticks = failed = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            try:
                result = orchestrator.tick()
            except Exception as exc:
                #come la GUI
                failed += 1
                logger.warning("Tick fallito, si riprova al prossimo intervallo: %s", exc)
                time.sleep(interval)
                continue
            ticks += 1
            if not args.quiet:
                print(_format_tick(result))
                #il primo tick non ha un intervallo precedente, quindi vale
                #sempre 0 Wh: e' atteso, non un errore
                if ticks == 1:
                    print("  (il primo tick non ha ancora un intervallo da integrare)")
                advice = result["advice"]
                label = "ricarica" if advice.target.value == "charge" else "attività pesanti"
                print(f"  {label}: {advice.message()}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
    finally:
        orchestrator.flush()

    report = orchestrator.build_daily_report()
    print(f"\ntick eseguiti   : {ticks}" + (f" ({failed} falliti)" if failed else ""))
    #gli stessi formati di finestra e storico
    print(
        f"oggi            : {format_energy(report.energy_wh)}, "
        f"{format_gco2(report.operational_gco2)} CO2eq"
    )
    print(f"ore attive      : {report.active_hours:.2f} h")
    charges = format_phone_charges(report.equivalent_phone_charges)
    print(f"equivalente     : {charges} ricariche di smartphone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
