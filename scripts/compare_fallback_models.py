# Confronto dei modelli di stima contro la misura reale

# Valuta i tre livelli di ripiego sulle righe gia' misurate del database:

#     ripiego generico   TDP costante per utilizzo di CPU
#     backend parziale   GPU dedicata misurata + CPU da TDP generico
#     CPU calibrata      GPU dedicata misurata + modello tarato su questa macchina

#     python scripts/compare_fallback_models.py


import argparse
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ecomonitor.sensors.calibration import fit_calibration
from ecomonitor.sensors.fallback_model import DEFAULT_TDP_W
from ecomonitor.storage.db import LocalDatabase, default_database_path

BANDS = ((0, 5), (5, 10), (10, 20), (20, 30), (30, 45), (45, 70), (70, 101))

def load_rows(db: LocalDatabase, max_row_seconds: float) -> list[tuple[float, float, float]]:
    #(utilizzo di CPU, potenza del dominio CPU, potenza totale) 
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)) as conn:
        raw = conn.execute(
            "SELECT cpu_percent, cpu_wh, wh, duration_seconds FROM energy_events "
            "WHERE is_estimated = 0 AND cpu_percent IS NOT NULL "
            "AND cpu_wh IS NOT NULL AND cpu_wh > 0 "
            "AND duration_seconds IS NOT NULL AND duration_seconds > 0 "
            "AND duration_seconds <= ?",
            (max_row_seconds,),
        ).fetchall()

    rows = []
    for cpu, cpu_wh, wh, seconds in raw:
        hours = seconds / 3600.0
        cpu_w, total_w = cpu_wh / hours, wh / hours
        if 0.0 <= cpu <= 100.0 and total_w > 0 and cpu_w <= total_w:
            rows.append((cpu, cpu_w, total_w))
    return rows


def errors(rows, predict) -> list[float]:
    #errore percentuale riga per riga
    out = []
    for cpu, cpu_w, total_w in rows:
        gpu_w = total_w - cpu_w
        out.append(abs(predict(cpu, gpu_w) - total_w) / total_w * 100.0)
    return out

def generic(cpu, _gpu_w):
    return DEFAULT_TDP_W * (cpu / 100.0)

def partial(cpu, gpu_w):
    return gpu_w + DEFAULT_TDP_W * (cpu / 100.0)

def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita e non __doc__: l'intestazione di questo file e' fatta
    #di commenti, quindi __doc__ vale None e .split() solleverebbe AttributeError
    parser = argparse.ArgumentParser(
        description="Confronto dei modelli di stima contro la misura reale"
    )
    parser.add_argument("--db", default=None, help="database da leggere")
    parser.add_argument("--repeats", type=int, default=20,
                        help="quante divisioni casuali (default 20)")
    parser.add_argument("--train", type=float, default=0.7,
                        help="quota di righe per la calibrazione (default 0.7)")
    parser.add_argument("--max-row-seconds", type=float, default=900.0,
                        help="scarta le righe che coprono piu' di tanti secondi")
    args = parser.parse_args(argv)

    path = Path(args.db) if args.db else default_database_path()
    rows = load_rows(LocalDatabase(path), args.max_row_seconds)

    print(f"database : {path}")
    print(f"righe    : {len(rows)} misurate, con utilizzo di CPU e dominio CPU")
    if len(rows) < 40:
        print("\nTroppo poche per un confronto: serve almeno una sessione elevata")
        print("di qualche decina di minuti con carico vario.")
        return 1

    cpus = [c for c, _, _ in rows]
    print(f"utilizzo : da {min(cpus):.1f}% a {max(cpus):.1f}%\n")

    rng = random.Random(20260826)
    scores: dict[str, list[float]] = {"generico": [], "parziale": [], "calibrato": []}
    worst: dict[str, list[float]] = {k: [] for k in scores}
    skipped = 0

    for _ in range(args.repeats):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        cut = int(len(shuffled) * args.train)
        train, test = shuffled[:cut], shuffled[cut:]

        model = fit_calibration([(c, w) for c, w, _ in train])
        if model is None:
            skipped += 1
            continue

        for name, predict in (
            ("generico", generic),
            ("parziale", partial),
            ("calibrato", lambda cpu, gpu_w, m=model: gpu_w + m.predict(cpu)),
        ):
            errs = errors(test, predict)
            scores[name].append(statistics.median(errs))
            worst[name].append(max(errs))

    if not scores["calibrato"]:
        print("Nessuna divisione ha prodotto una calibrazione sostenibile.")
        return 1

    print(f"=== errore sul totale, su righe mai viste dal modello "
          f"({args.repeats - skipped} divisioni {args.train:.0%}/{1 - args.train:.0%}) ===")
    print(f"{'modello':>12} {'mediano':>10} {'peggiore':>10}")
    print("-" * 34)
    for name in ("generico", "parziale", "calibrato"):
        print(f"{name:>12} {statistics.fmean(scores[name]):>9.1f}% "
              f"{statistics.fmean(worst[name]):>9.1f}%")

    whole = fit_calibration([(c, w) for c, w, _ in rows])
    print(f"\n=== modello calibrato su tutte le righe ===\n  {whole.describe()}")

    print("\n=== errore mediano per fascia di utilizzo ===")
    print(f"{'fascia':>10} {'righe':>7} {'generico':>10} {'parziale':>10} {'calibrato':>11}")
    print("-" * 52)
    for lo, hi in BANDS:
        band = [r for r in rows if lo <= r[0] < hi]
        if not band:
            continue
        values = []
        for predict in (generic, partial, lambda cpu, gpu_w: gpu_w + whole.predict(cpu)):
            values.append(statistics.median(errors(band, predict)))
        print(f"{f'{lo}-{hi}%':>10} {len(band):>7} {values[0]:>9.0f}% "
              f"{values[1]:>9.0f}% {values[2]:>10.0f}%")

    print("\nNOTA: l'ultima tabella e' calcolata su un modello tarato anche su quelle")
    print("righe, quindi e' ottimistica. Il numero difendibile e' quello in alto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
