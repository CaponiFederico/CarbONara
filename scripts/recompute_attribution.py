#Ricalcolo offline dell'attribuzione con i due modelli 

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ecomonitor.carbon.attribution import (
    AttributionSource,
    CarbonAttributionService,
)
from ecomonitor.carbon.battery_pool import AttributionModel, BatteryCarbonPool
from ecomonitor.storage.db import LocalDatabase, TraceEvent, default_database_path

MIN_BATTERY_ROWS = 30
MIN_CHARGE_EVENTS = 3
MIN_DISTINCT_INTENSITIES = 2
#Sotto queste soglie il confronto e' vuoto

@dataclass
class ReplayResult:
    model: AttributionModel
    total_gco2: float = 0.0
    total_wh: float = 0.0
    gco2_by_day: dict[date, float] = field(default_factory=lambda: defaultdict(float))
    battery_gco2: float = 0.0


def replay(trace: list[TraceEvent], model: AttributionModel) -> ReplayResult:
    #Riusa il servizio di attribuzione vero invece di riscriverne la regola
    pool = BatteryCarbonPool(model=model)
    service = CarbonAttributionService(pool)
    result = ReplayResult(model=model)

    for event in trace:
        #le ricariche sono state registrate come eventi 
        #separati, ma il pool le vuole tutte insieme

        for charge in event.charges:
            pool.charge(charge.percent_charged, charge.intensity_g_per_kwh)

        drained = event.discharged_percent or 0.0

        #charged e' gia' stato applicato sopra: qui si passa solo la scarica
        outcome = service.attribute(
            delta_wh=event.wh,
            is_plugged_in=event.is_plugged,
            battery_percent=event.battery_percent,
            previous_battery_percent=(
                None if event.battery_percent is None else event.battery_percent + drained
            ),
            grid_intensity_g_per_kwh=event.recorded_intensity_g_per_kwh or 0.0,
            unknown_origin_intensity_g_per_kwh=event.unknown_origin_intensity_g_per_kwh,
        )

        result.total_gco2 += outcome.gco2
        result.total_wh += event.wh
        result.gco2_by_day[event.timestamp.astimezone().date()] += outcome.gco2
        if outcome.source is not AttributionSource.GRID_LIVE:
            result.battery_gco2 += outcome.gco2

    return result


@dataclass
class Adequacy:
    total_rows: int
    battery_rows: int
    charge_events: int
    distinct_intensities: int
    reconstructed_charges: int

    @property
    def blocking_reasons(self) -> list[str]:
        reasons = []
        if self.total_rows == 0:
            reasons.append("nessun intervallo di consumo registrato")
        if self.battery_rows == 0:
            reasons.append(
                "nessun consumo a batteria: il pool non viene mai interrogato, "
                "quindi i due modelli non possono che coincidere"
            )
        elif self.battery_rows < MIN_BATTERY_ROWS:
            reasons.append(
                f"solo {self.battery_rows} intervalli a batteria (indicativamente "
                f"ne servono almeno {MIN_BATTERY_ROWS})"
            )
        if self.charge_events < MIN_CHARGE_EVENTS:
            reasons.append(
                f"solo {self.charge_events} ricariche registrate (almeno {MIN_CHARGE_EVENTS})"
            )
        if self.distinct_intensities < MIN_DISTINCT_INTENSITIES:
            reasons.append(
                "le ricariche hanno tutte la stessa intensita': media ponderata e FIFO "
                "mediano lo stesso numero e coincidono per costruzione"
            )
        return reasons


def measure_adequacy(trace: list[TraceEvent]) -> Adequacy:
    charges = [charge for event in trace for charge in event.charges]
    return Adequacy(
        total_rows=len(trace),
        battery_rows=sum(1 for event in trace if not event.is_plugged),
        charge_events=len(charges),
        distinct_intensities=len({round(c.intensity_g_per_kwh, 1) for c in charges}),
        reconstructed_charges=sum(1 for c in charges if c.is_reconstructed),
    )


def _parse_day(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone() if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recompute_attribution",
        description="Confronta media ponderata e FIFO sulle tracce gia' registrate.",
    )
    parser.add_argument("--db", default=None, help="database da leggere")
    parser.add_argument("--from", dest="start", default=None, help="data iniziale (AAAA-MM-GG)")
    parser.add_argument("--to", dest="end", default=None, help="data finale esclusa")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_database_path()
    if not db_path.exists():
        print(f"database non trovato: {db_path}")
        return 2

    db = LocalDatabase(db_path)
    trace = db.iter_attribution_trace(_parse_day(args.start), _parse_day(args.end))

    print(f"database : {db_path}")
    print(f"intervalli letti: {len(trace)}")
    if trace:
        print(f"periodo  : da {trace[0].timestamp.astimezone():%Y-%m-%d %H:%M} "
              f"a {trace[-1].timestamp.astimezone():%Y-%m-%d %H:%M}")

    adequacy = measure_adequacy(trace)
    print()
    print("-- consistenza dei dati ---------------------------------------------")
    print(f"  intervalli a batteria   : {adequacy.battery_rows} su {adequacy.total_rows}")
    print(f"  ricariche registrate    : {adequacy.charge_events}"
          f" ({adequacy.reconstructed_charges} ricostruite)")
    print(f"  intensita' di ricarica distinte: {adequacy.distinct_intensities}")

    reasons = adequacy.blocking_reasons
    if reasons:
        print()
        print("  IL CONFRONTO NON E' SIGNIFICATIVO:")
        for reason in reasons:
            print(f"    - {reason}")

    weighted = replay(trace, AttributionModel.WEIGHTED_AVERAGE)
    fifo = replay(trace, AttributionModel.FIFO)

    print()
    print("-- confronto fra i modelli ------------------------------------------")
    print(f"  energia totale          : {weighted.total_wh:.2f} Wh")
    print(f"  media ponderata         : {weighted.total_gco2:.3f} gCO2eq"
          f"  (di cui da batteria {weighted.battery_gco2:.3f})")
    print(f"  FIFO                    : {fifo.total_gco2:.3f} gCO2eq"
          f"  (di cui da batteria {fifo.battery_gco2:.3f})")

    difference = fifo.total_gco2 - weighted.total_gco2
    if weighted.total_gco2 > 0:
        print(f"  differenza              : {difference:+.3f} g"
              f"  ({difference / weighted.total_gco2 * 100:+.2f}%)")
    else:
        print(f"  differenza              : {difference:+.3f} g")

    days = sorted(set(weighted.gco2_by_day) | set(fifo.gco2_by_day))
    if len(days) > 1:
        print()
        print("-- per giornata ------------------------------------------------------")
        print(f"  {'giorno':12} {'ponderata':>12} {'FIFO':>12} {'differenza':>12}")
        for day in days:
            w = weighted.gco2_by_day[day]
            f = fifo.gco2_by_day[day]
            print(f"  {day.isoformat():12} {w:12.3f} {f:12.3f} {f - w:+12.3f}")

    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
