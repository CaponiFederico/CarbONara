# Confronto fra le righe di CarbO'Nara e la presa di riferimento
#
#     P_presa = a * P_ecomonitor + b
#
#     python scripts/wattmeter/compare_plug.py --plug measurements/plug.csv
#     python scripts/wattmeter/compare_plug.py --plug measurements/plug.csv --db measurements/session01.db
#
import argparse
import csv
import sqlite3
import sys
from bisect import bisect_right
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ecomonitor.storage.db import default_database_path

#Oltre questo una lettura della presa non vale piu' come stato corrente
MAX_HOLD_SECONDS = 180.0

#Sotto questa copertura la finestra non e' confrontabile
MIN_COVERAGE = 0.9

#Righe piu' lunghe di cosi' mediano condizioni troppo diverse
MAX_ROW_SECONDS = 900.0

#Punti minimi in ciascuna meta' dell'intervallo di potenza 
MIN_POINTS_PER_HALF = 5


def _moment(text: str) -> datetime:
    #accetta sia gli ISO con offset scritti dal database sia una data locale
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def load_plug_samples(path: Path) -> list[tuple[datetime, float]]:
    samples = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            power = row.get("power_w")
            if not power:
                continue
            try:
                samples.append((_moment(row["timestamp_utc"]), float(power)))
            except (ValueError, KeyError):
                continue
    samples.sort(key=lambda sample: sample[0])
    return samples


def load_rows(db: Path, start: datetime | None, end: datetime | None,
              max_row: float) -> list[dict]:
    conditions = ["duration_seconds IS NOT NULL", "duration_seconds > 0",
                  "duration_seconds <= ?", "wh > 0"]
    parameters: list = [max_row]
    if start:
        conditions.append("timestamp >= ?")
        parameters.append(start.isoformat())
    if end:
        conditions.append("timestamp < ?")
        parameters.append(end.isoformat())

    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        raw_rows = conn.execute(
            "SELECT timestamp, wh, duration_seconds, cpu_percent, is_estimated "
            f"FROM energy_events WHERE {' AND '.join(conditions)} ORDER BY timestamp",
            parameters,
        ).fetchall()

    rows = []
    for text, wh, seconds, cpu, estimated in raw_rows:
        row_end = _moment(text)
        rows.append(
            {
                "start": row_end - timedelta(seconds=seconds),
                "end": row_end,
                "seconds": seconds,
                "watts": wh * 3600.0 / seconds,
                "wh": wh,
                "cpu": cpu,
                "estimated": bool(estimated),
            }
        )
    return rows


def plug_mean(samples: list[tuple[datetime, float]], start: datetime,
              end: datetime) -> tuple[float | None, float]:
    #Media pesata sul tempo della funzione a gradini definita dai campioni, piu'
    #la frazione di finestra effettivamente coperta

    if not samples or end <= start:
        return None, 0.0

    times = [sample[0] for sample in samples]
    #il campione che vale all'istante di inizio e' l'ultimo non successivo
    i = max(0, bisect_right(times, start) - 1)

    energy = 0.0
    covered = 0.0
    while i < len(samples) and samples[i][0] < end:
        moment, watts = samples[i]
        following = samples[i + 1][0] if i + 1 < len(samples) else None

        window_start = max(moment, start)
        expiry = moment + timedelta(seconds=MAX_HOLD_SECONDS)
        window_end = min(following, end, expiry) if following else min(end, expiry)

        if window_end > window_start:
            duration = (window_end - window_start).total_seconds()
            energy += watts * duration
            covered += duration
        i += 1

    total = (end - start).total_seconds()
    if covered <= 0:
        return None, 0.0
    return energy / covered, covered / total


def regression(pairs: list[tuple[float, float]]) -> dict | None:
    #minimi quadrati di presa = a * ecomonitor + b
    n = len(pairs)
    if n < 3:
        return None

    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    if sxx == 0:
        #tutti i punti allo stesso carico: pendenza e intercetta non sono
        #separabili
        return None

    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    a = sxy / sxx
    b = my - a * mx

    ss_tot = sum((y - my) ** 2 for _, y in pairs)
    ss_res = sum((y - (a * x + b)) ** 2 for x, y in pairs)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None

    residuals = [y - (a * x + b) for x, y in pairs]
    return {
        "a": a,
        "b": b,
        "r2": r2,
        "n": n,
        "mean_absolute_residual": sum(abs(r) for r in residuals) / n,
        "max_residual": max(abs(r) for r in residuals),
    }


def print_summary(pairs: list[tuple[float, float]], rows: list[dict],
                  coverages: list[float]) -> None:
    eco = [x for x, _ in pairs]
    plug = [y for _, y in pairs]

    print(f"\nFinestre confrontabili: {len(pairs)}")
    print(f"  copertura media della presa: {sum(coverages) / len(coverages) * 100:.1f}%")
    print(f"  CarbO'Nara: da {min(eco):.2f} a {max(eco):.2f} W, media {sum(eco) / len(eco):.2f} W")
    print(f"  presa:      da {min(plug):.2f} a {max(plug):.2f} W, "
          f"media {sum(plug) / len(plug):.2f} W")

    ratios = [y / x for x, y in pairs if x > 0]
    if ratios:
        ratios.sort()
        print(f"  rapporto presa/CarbO'Nara: mediana {ratios[len(ratios) // 2]:.2f}")

    model = regression(pairs)
    if model is None:
        print("\nRegressione non calcolabile: servono almeno 3 finestre a carichi diversi.")
    else:
        r2 = f"{model['r2']:.3f}" if model["r2"] is not None else "n/d"
        print("\nP_presa = a * P_ecomonitor + b")
        print(f"  a  = {model['a']:.3f}")
        print(f"  b  = {model['b']:.2f} W   (base di piattaforma)")
        print(f"  R2 = {r2}")
        print(f"  residuo medio assoluto {model['mean_absolute_residual']:.2f} W, "
              f"massimo {model['max_residual']:.2f} W")

    low, high = min(eco), max(eco)
    middle = (low + high) / 2.0
    below = sum(1 for x in eco if x < middle)
    above = len(eco) - below
    if below < MIN_POINTS_PER_HALF or above < MIN_POINTS_PER_HALF:
        print(
            f"\nAttenzione: {below} finestre sotto {middle:.0f} W e {above} sopra. "
            f"Con meno di {MIN_POINTS_PER_HALF} punti da una parte, pendenza e "
            "intercetta non sono separabili: i coefficienti qui sopra non sono "
            "confrontabili con quelli di un'altra sessione."
        )

    #l'energia integrata e' la metrica piu' robusta
    wh_eco = sum(row["wh"] for row in rows)
    wh_plug = sum(y * row["seconds"] / 3600.0 for (_, y), row in zip(pairs, rows, strict=True))
    if wh_eco > 0:
        print(f"\nEnergia sulle stesse finestre: CarbO'Nara {wh_eco:.2f} Wh, "
              f"presa {wh_plug:.2f} Wh (rapporto {wh_plug / wh_eco:.2f})")

    estimated_rows = sum(1 for row in rows if row["estimated"])
    if estimated_rows:
        print(f"\nAttenzione: {estimated_rows} finestre su {len(rows)} sono stimate "
              "(is_estimated=1), non misurate. Vanno analizzate a parte")


def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita
    parser = argparse.ArgumentParser(
        description="Allinea la serie della presa con le righe di CarbO'Nara e le confronta"
    )
    parser.add_argument("--plug", required=True, help="CSV scritto da read_plug.py")
    parser.add_argument("--db", default=None, help="database da leggere")
    parser.add_argument("--from", dest="start", default=None, help="inizio finestra (ISO 8601)")
    parser.add_argument("--to", dest="end", default=None, help="fine finestra (ISO 8601)")
    parser.add_argument("--max-row-seconds", type=float, default=MAX_ROW_SECONDS)
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=MIN_COVERAGE,
        help="frazione minima di finestra coperta dalla presa (0-1)",
    )
    parser.add_argument("--pairs", default=None, help="scrive le coppie appaiate in questo CSV")
    args = parser.parse_args(argv)

    plug_path = Path(args.plug)
    if not plug_path.exists():
        parser.error(f"{plug_path} non esiste")

    db = Path(args.db) if args.db else default_database_path()
    if not db.exists():
        parser.error(f"{db} non esiste")

    samples = load_plug_samples(plug_path)
    if not samples:
        print(f"{plug_path} non contiene letture di potenza.", file=sys.stderr)
        return 1

    start = _moment(args.start) if args.start else None
    end = _moment(args.end) if args.end else None
    rows = load_rows(db, start, end, args.max_row_seconds)
    if not rows:
        print("Nessuna riga di energy_events nella finestra richiesta.", file=sys.stderr)
        return 1

    print(f"Presa:      {len(samples)} letture, "
          f"da {samples[0][0].isoformat()} a {samples[-1][0].isoformat()}")
    print(f"CarbO'Nara: {len(rows)} righe, "
          f"da {rows[0]['start'].isoformat()} a {rows[-1]['end'].isoformat()}")

    pairs: list[tuple[float, float]] = []
    matched: list[dict] = []
    coverages: list[float] = []
    dropped = 0

    for row in rows:
        plug_watts, coverage = plug_mean(samples, row["start"], row["end"])
        if plug_watts is None or coverage < args.min_coverage:
            dropped += 1
            continue
        pairs.append((row["watts"], plug_watts))
        coverages.append(coverage)
        matched.append({**row, "plug_watts": plug_watts, "coverage": coverage})

    if dropped:
        print(f"  {dropped} righe scartate: la presa non copre la loro finestra")

    if not pairs:
        print(
            "\nNessuna finestra in comune. Le due serie non si sovrappongono, "
            "oppure gli orologi delle due macchine non sono allineati.",
            file=sys.stderr,
        )
        return 1

    print_summary(pairs, matched, coverages)

    if args.pairs:
        destination = Path(args.pairs)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["start_utc", "end_utc", "watts_ecomonitor", "watts_plug",
                 "cpu_percent", "estimated", "coverage"]
            )
            for row in matched:
                writer.writerow([
                    row["start"].isoformat(), row["end"].isoformat(),
                    f"{row['watts']:.4f}", f"{row['plug_watts']:.4f}",
                    row["cpu"], int(row["estimated"]), f"{row['coverage']:.3f}",
                ])
        print(f"\nCoppie appaiate scritte in {destination}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
