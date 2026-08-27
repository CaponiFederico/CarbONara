#Emissioni evitate spostando il consumo nel tempo

#Per ogni riga di consumo confronta l'intensita' pagata con quella che la rete
#aveva in quel momento. La differenza e' quello che si sarebbe pagato tirando la
#stessa energia dalla presa in quell'istante

#    evitato = wh / 1000 * (rete in quel momento - intensita' pagata)

#    python scripts/avoided_emissions.py

import argparse
import statistics
import sys
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ecomonitor.history import (
    AVOIDED_MAX_MATCH_SECONDS,
    GRID_ATTRIBUTION_SOURCES,
    LIVE_SAMPLE_SOURCE,
    MIN_AVOIDED_ROWS,
    compute_avoided,
)
from ecomonitor.reporting import format_energy, format_gco2
from ecomonitor.storage.db import EnergySample, GridSample, LocalDatabase, default_database_path

#Finestra letta quando non se ne chiede una
DEFAULT_DAYS = 3650


def nearest_sample(
    grid_samples: list[GridSample], moment: datetime, max_seconds: float
) -> GridSample | None:
    #Il campione piu' vicino
    if not grid_samples:
        return None

    index = bisect_left([sample.timestamp for sample in grid_samples], moment)
    candidates = []
    if index < len(grid_samples):
        candidates.append(grid_samples[index])
    if index > 0:
        candidates.append(grid_samples[index - 1])

    best = min(candidates, key=lambda sample: abs((sample.timestamp - moment).total_seconds()))
    if abs((best.timestamp - moment).total_seconds()) > max_seconds:
        return None
    return best


def grid_row_residual(
    samples: list[EnergySample], grid_samples: list[GridSample], max_seconds: float
) -> tuple[list[float], float, float, int]:
    deltas: list[float] = []
    residual = grid_wh = 0.0
    rows = 0

    for sample in samples:
        if sample.wh <= 0 or sample.attribution_source not in GRID_ATTRIBUTION_SOURCES:
            continue
        nearest = nearest_sample(grid_samples, sample.timestamp, max_seconds)
        if nearest is None or nearest.source != LIVE_SAMPLE_SOURCE:
            continue

        paid = sample.gco2 / sample.wh * 1000.0
        deltas.append(abs(nearest.g_per_kwh - paid))
        residual += sample.wh / 1000.0 * (nearest.g_per_kwh - paid)
        grid_wh += sample.wh
        rows += 1

    return deltas, residual, grid_wh, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Emissioni evitate spostando il consumo")
    parser.add_argument("--db", type=Path, default=None, help="percorso del database")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="giorni da leggere")
    parser.add_argument(
        "--max-match-seconds",
        type=float,
        default=AVOIDED_MAX_MATCH_SECONDS,
        help="distanza massima fra riga e campione di rete",
    )
    args = parser.parse_args()

    db = LocalDatabase(args.db or default_database_path())
    if not Path(db.path).exists():
        print(f"Database non trovato: {db.path}")
        raise SystemExit(1)

    end = datetime.now().astimezone()
    start = end - timedelta(days=args.days)
    samples = db.energy_samples_between(start, end)
    grid_samples = db.grid_intensity_samples_between(start, end)

    print(f"Database: {db.path}")
    print(f"Righe di consumo: {len(samples)}   campioni di rete: {len(grid_samples)}")
    print()

    if not samples:
        print("Nessuna riga di consumo.")
        return

    avoided = compute_avoided(samples, grid_samples, args.max_match_seconds)

    print("Emissioni evitate dall'energia presa dalla batteria")
    if not avoided.has_battery:
        print("  Nessuna riga presa dalla batteria.")
        print("  Su una macchina sempre collegata questo numero non esiste.")
    elif avoided.rows < MIN_AVOIDED_ROWS:
        print(f"  Solo {avoided.rows} righe da collegato, ne servono {MIN_AVOIDED_ROWS}.")
    elif not avoided.is_publishable:
        print(
            f"  Solo il {avoided.matched_share * 100:.0f}% dell'energia da batteria e' "
            f" da collegato: saldo non pubblicato."
        )
    else:
        print(f"  righe                {avoided.rows} ({format_energy(avoided.wh)})")
        print(f"  evitato              {format_gco2(avoided.saved_gco2)}")
        print(f"  emesso in piu'       {format_gco2(avoided.lost_gco2)}")
        print(f"  saldo                {format_gco2(abs(avoided.balance_gco2))}")
        print("  Al lordo delle perdite di carica e scarica della batteria.")

    if avoided.unmatched_wh > 0:
        print(f"  fuori: {format_energy(avoided.unmatched_wh)} senza lettura vicina")
    if avoided.partial_wh > 0:
        print(f"  fuori: {format_energy(avoided.partial_wh)} senza storico di ricarica")
    print()

    deltas, residual, grid_wh, rows = grid_row_residual(
        samples, grid_samples, args.max_match_seconds
    )
    print("Controllo sulle righe prese dalla rete (atteso: zero)")
    if not rows:
        print("  Nessuna riga presa dalla rete.")
        return

    print(f"  righe                {rows} ({format_energy(grid_wh)})")
    print(
        f"  scarto di intensita' mediano {statistics.median(deltas):.1f} gCO2/kWh, "
        f"massimo {max(deltas):.1f}"
    )
    print(f"  residuo sul totale   {format_gco2(abs(residual))} su {format_energy(grid_wh)}")
    print("  E' l'errore dell'aggancio al campione piu' vicino, non un risparmio.")


if __name__ == "__main__":
    main()
