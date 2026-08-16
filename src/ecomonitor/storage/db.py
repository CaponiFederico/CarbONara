import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from ecomonitor.carbon.battery_pool import BatteryCarbonPool

logger = logging.getLogger(__name__)

#Data store locale SQLite:
#- energy_events: consumo per intervallo, con i gCO2 gia' attribuiti e il
#  movimento di carica dell'intervallo
#- charge_events: ricariche in punti percentuali, audit trail del pool
#- battery_pool_state: senza questa tabella il pool si azzera a ogni riavvio
#- grid_intensity_samples: serie storica dell'intensita' di rete osservata
#- daily_summary: aggregati giornalieri

SCHEMA = """
CREATE TABLE IF NOT EXISTS energy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    wh REAL NOT NULL,
    is_plugged INTEGER NOT NULL,
    gco2 REAL NOT NULL,
    is_estimated INTEGER NOT NULL,
    intensity_g_per_kwh REAL,
    attribution_source TEXT,
    battery_percent REAL,
    charged_percent REAL,
    discharged_percent REAL,
    pool_coverage_ratio REAL,
    battery_assisted INTEGER,
    n_samples INTEGER,
    duration_seconds REAL,
    unknown_origin_intensity_g_per_kwh REAL,
    unknown_origin_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_energy_events_timestamp
    ON energy_events(timestamp);

CREATE TABLE IF NOT EXISTS charge_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    percent_charged REAL NOT NULL,
    grid_intensity_g_per_kwh REAL NOT NULL,
    is_reconstructed INTEGER NOT NULL DEFAULT 0,
    intensity_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_charge_events_timestamp
    ON charge_events(timestamp);

CREATE TABLE IF NOT EXISTS battery_pool_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT NOT NULL,
    pool_json TEXT NOT NULL,
    last_battery_percent REAL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS grid_intensity_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    zone TEXT NOT NULL,
    g_per_kwh REAL NOT NULL,
    source TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grid_samples_timestamp
    ON grid_intensity_samples(timestamp);

CREATE INDEX IF NOT EXISTS idx_grid_samples_zone_timestamp
    ON grid_intensity_samples(zone, timestamp);

CREATE TABLE IF NOT EXISTS daily_summary (
    day TEXT PRIMARY KEY,
    energy_wh REAL NOT NULL,
    operational_gco2 REAL NOT NULL,
    active_hours REAL NOT NULL
);
"""


@dataclass(frozen=True)
class ChargeRecord:
    #una ricarica registrata, con la sua intensita' esatta e la provenienza di
    #quel numero

    timestamp: datetime
    percent_charged: float
    intensity_g_per_kwh: float
    is_reconstructed: bool
    intensity_source: str | None


@dataclass(frozen=True)
class TraceEvent:
    #un intervallo con tutto cio' che serve a rieseguire l'attribuzione con un
    #modello diverso

    timestamp: datetime
    wh: float
    is_plugged: bool
    is_estimated: bool

    battery_percent: float | None
    charged_percent: float | None
    discharged_percent: float | None

    charges: tuple[ChargeRecord, ...]
    #le ricariche cadute dentro l'intervallo: una riga aggregata al minuto puo'
    #contenere piu' punti caricati a intensita' diverse

    recorded_intensity_g_per_kwh: float | None
    recorded_gco2: float
    recorded_source: str | None
    pool_coverage_ratio: float | None
    battery_assisted: bool | None
    n_samples: int | None

    unknown_origin_intensity_g_per_kwh: float | None = None
    #Il riferimento applicato all'energia gia' in batteria di cui non si conosce
    #l'istante di ricarica
    unknown_origin_source: str | None = None
    #reconstructed_api, uncovered_local o static_country_mean

    @property
    def charge_intensity_g_per_kwh(self) -> float | None:
        #media pesata delle ricariche dell'intervallo, comoda per riassumere:
        #il ricalcolo deve usare charges
        total = sum(charge.percent_charged for charge in self.charges)
        if total <= 0:
            return None
        return (
            sum(charge.percent_charged * charge.intensity_g_per_kwh for charge in self.charges)
            / total
        )


def to_utc_iso(moment: datetime) -> str:
    #le righe vengono confrontate con standard utc 
    return moment.astimezone(UTC).isoformat()


def default_database_path() -> Path:
    #GUI, CLI e script di ricalcolo devono aprire lo stesso file,
    #altrimenti il lock che li tiene separati protegge percorsi diversi
    import platformdirs

    return Path(platformdirs.user_data_path("ecomonitor")) / "local.db"


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    #Istanti di inizio e fine di un giorno di calendario locale

    #caso di cambio dell'ora legale in alcuni paesi
 
    start = datetime.combine(day, dt_time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), dt_time.min).astimezone()
    return start, end


class LocalDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    #Nessuna migrazione di schema TODO rivedere prima di rilasciare: 
    # il database e' in beta e non ci sono ancora utenti reali

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # -- eventi di consumo -------------------------------------------------

    def record_energy_event(
        self,
        timestamp: datetime,
        wh: float,
        is_plugged: bool,
        gco2: float,
        is_estimated: bool,
        intensity_g_per_kwh: float | None = None,
        attribution_source: str | None = None,
        battery_percent: float | None = None,
        charged_percent: float | None = None,
        discharged_percent: float | None = None,
        pool_coverage_ratio: float | None = None,
        battery_assisted: bool | None = None,
        n_samples: int | None = None,
        duration_seconds: float | None = None,
        unknown_origin_intensity_g_per_kwh: float | None = None,
        unknown_origin_source: str | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO energy_events (timestamp, wh, is_plugged, gco2, is_estimated, "
                "intensity_g_per_kwh, attribution_source, battery_percent, charged_percent, "
                "discharged_percent, pool_coverage_ratio, battery_assisted, n_samples, "
                "duration_seconds, unknown_origin_intensity_g_per_kwh, unknown_origin_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    to_utc_iso(timestamp),
                    wh,
                    int(is_plugged),
                    gco2,
                    int(is_estimated),
                    intensity_g_per_kwh,
                    attribution_source,
                    battery_percent,
                    charged_percent,
                    discharged_percent,
                    pool_coverage_ratio,
                    None if battery_assisted is None else int(battery_assisted),
                    n_samples,
                    duration_seconds,
                    unknown_origin_intensity_g_per_kwh,
                    unknown_origin_source,
                ),
            )
            conn.commit()

    def record_buffered_energy_row(self, row) -> None:
        #scrive una riga gia' aggregata da EnergyEventBuffer
        self.record_energy_event(
            row.timestamp,
            row.wh,
            row.is_plugged,
            row.gco2,
            row.is_estimated,
            intensity_g_per_kwh=row.intensity_g_per_kwh,
            attribution_source=row.attribution_source,
            battery_percent=row.battery_percent,
            charged_percent=row.charged_percent,
            discharged_percent=row.discharged_percent,
            pool_coverage_ratio=row.pool_coverage_ratio,
            battery_assisted=row.battery_assisted,
            n_samples=row.n_samples,
            duration_seconds=(row.timestamp - row.started_at).total_seconds(),
            unknown_origin_intensity_g_per_kwh=row.unknown_origin_intensity_g_per_kwh,
            unknown_origin_source=row.unknown_origin_source,
        )

    def sum_energy_events_for_day(self, day: date) -> dict:
        #day e' un giorno di calendario locale: i timestamp sono in UTC, quindi
        #si confrontano i due estremi convertiti
        start_local, end_local = local_day_bounds(day)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(wh), 0), COALESCE(SUM(gco2), 0), COUNT(*) "
                "FROM energy_events WHERE timestamp >= ? AND timestamp < ?",
                (to_utc_iso(start_local), to_utc_iso(end_local)),
            ).fetchone()
            return {"energy_wh": row[0], "operational_gco2": row[1], "n_events": row[2]}

    def active_seconds_for_day(self, day: date, max_gap_seconds: float) -> float:
        #Secondi di utilizzo attivo, ricostruiti dagli intervalli fra eventi
        #consecutivi invece che da un contatore in memoria: un riavvio a meta'
        #giornata azzererebbe il contatore mentre i gCO2 restano sul database.
        #Gli intervalli oltre max_gap_seconds sono finestre non osservate e
        #vengono esclusi, non estrapolati. duration_seconds copre il tempo
        #dentro una riga aggregata, che serve alla prima riga di ogni sessione
        start_local, end_local = local_day_bounds(day)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT timestamp, duration_seconds FROM energy_events "
                "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (to_utc_iso(start_local), to_utc_iso(end_local)),
            ).fetchall()

        total = 0.0
        previous: datetime | None = None
        for raw, duration in rows:
            moment = datetime.fromisoformat(raw)
            own = duration or 0.0
            if previous is None:
                total += own
            else:
                elapsed = (moment - previous).total_seconds()
                if 0 < elapsed <= max_gap_seconds:
                    total += elapsed
                else:
                    total += own  # salto: la riga apre una nuova sessione
            previous = moment
        return total

    def iter_attribution_trace(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[TraceEvent]:
        #Traccia ordinata con cui ricalcolare l'attribuzione offline, unendo
        #energy_events e charge_events. I Delta% di scarica sono una colonna
        #perche' non sono ricavabili a posteriori: la differenza fra due
        #battery_percent consecutivi e' ambigua attraverso una sospensione o
        #una riga mancante
        conditions = []
        params: list[str] = []
        if start is not None:
            conditions.append("timestamp >= ?")
            params.append(to_utc_iso(start))
        if end is not None:
            conditions.append("timestamp < ?")
            params.append(to_utc_iso(end))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            energy_rows = conn.execute(
                f"SELECT * FROM energy_events {where} ORDER BY timestamp, id",
                params,
            ).fetchall()
            charge_rows = conn.execute(
                f"SELECT * FROM charge_events {where} ORDER BY timestamp, id",
                params,
            ).fetchall()

        #una riga di consumo puo' aggregare piu' tick: le ricariche si
        #assegnano all'intervallo (riga precedente, riga corrente]
        charge_index = 0
        previous_ts: str | None = None
        trace: list[TraceEvent] = []

        for row in energy_rows:
            ts = row["timestamp"]
            charges: list[ChargeRecord] = []
            while charge_index < len(charge_rows) and charge_rows[charge_index]["timestamp"] <= ts:
                charge = charge_rows[charge_index]
                if previous_ts is None or charge["timestamp"] > previous_ts:
                    charges.append(
                        ChargeRecord(
                            timestamp=datetime.fromisoformat(charge["timestamp"]),
                            percent_charged=charge["percent_charged"],
                            intensity_g_per_kwh=charge["grid_intensity_g_per_kwh"],
                            is_reconstructed=bool(charge["is_reconstructed"]),
                            intensity_source=charge["intensity_source"],
                        )
                    )
                charge_index += 1

            trace.append(
                TraceEvent(
                    timestamp=datetime.fromisoformat(ts),
                    wh=row["wh"],
                    is_plugged=bool(row["is_plugged"]),
                    is_estimated=bool(row["is_estimated"]),
                    battery_percent=row["battery_percent"],
                    charged_percent=row["charged_percent"],
                    discharged_percent=row["discharged_percent"],
                    charges=tuple(charges),
                    recorded_intensity_g_per_kwh=row["intensity_g_per_kwh"],
                    recorded_gco2=row["gco2"],
                    recorded_source=row["attribution_source"],
                    pool_coverage_ratio=row["pool_coverage_ratio"],
                    battery_assisted=(
                        None if row["battery_assisted"] is None else bool(row["battery_assisted"])
                    ),
                    n_samples=row["n_samples"],
                    unknown_origin_intensity_g_per_kwh=row["unknown_origin_intensity_g_per_kwh"],
                    unknown_origin_source=row["unknown_origin_source"],
                )
            )
            previous_ts = ts
        return trace

    # -- ricariche ---------------------------------------------------------

    def record_charge_event(
        self,
        timestamp: datetime,
        percent_charged: float,
        grid_intensity_g_per_kwh: float,
        is_reconstructed: bool = False,
        intensity_source: str | None = None,
    ) -> None:
        #is_reconstructed dice quando la ricarica e' stata stimata (app chiusa),
        #intensity_source quanto vale il numero: live, static_fallback,
        #reconstructed_api o reconstructed_local. Sono ortogonali
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO charge_events (timestamp, percent_charged, "
                "grid_intensity_g_per_kwh, is_reconstructed, intensity_source) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    to_utc_iso(timestamp),
                    percent_charged,
                    grid_intensity_g_per_kwh,
                    int(is_reconstructed),
                    intensity_source,
                ),
            )
            conn.commit()

    def count_charge_events(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM charge_events").fetchone()[0]

    # -- stato del pool ----------------------------------------------------

    def save_pool_state(
        self,
        pool: BatteryCarbonPool,
        last_battery_percent: float | None,
        last_seen_at: datetime,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO battery_pool_state (id, updated_at, pool_json, "
                "last_battery_percent, last_seen_at) VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
                "pool_json=excluded.pool_json, "
                "last_battery_percent=excluded.last_battery_percent, "
                "last_seen_at=excluded.last_seen_at",
                (
                    to_utc_iso(datetime.now(UTC)),
                    json.dumps(pool.to_dict()),
                    last_battery_percent,
                    to_utc_iso(last_seen_at),
                ),
            )
            conn.commit()

    def load_pool_state(self) -> tuple[BatteryCarbonPool | None, float | None, datetime | None]:
        #(pool, ultima percentuale vista, istante dell'ultima lettura), tutti
        #None alla prima esecuzione
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT pool_json, last_battery_percent, last_seen_at "
                "FROM battery_pool_state WHERE id = 1"
            ).fetchone()

        if row is None:
            return None, None, None


        #Si riparte dal pool vuoto: si perde lo storico di ricarica, non l'avvio
        try:
            pool = BatteryCarbonPool.from_dict(json.loads(row[0]))
            last_seen = datetime.fromisoformat(row[2]) if row[2] else None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Stato del pool non leggibile (%s): si riparte senza storico di ricarica. "
                "Il consumo a batteria sara' attribuito alla media nazionale finche' non "
                "si osserva una nuova ricarica",
                exc,
            )
            return None, None, None

        return pool, row[1], last_seen

    # -- storico intensita' di rete ----------------------------------------

    def record_grid_intensity(
        self, timestamp: datetime, zone: str, g_per_kwh: float, source: str
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO grid_intensity_samples (timestamp, zone, g_per_kwh, source) "
                "VALUES (?, ?, ?, ?)",
                (to_utc_iso(timestamp), zone, g_per_kwh, source),
            )
            conn.commit()

    def mean_grid_intensity_between(
        self, start: datetime, end: datetime, zone: str | None = None
    ) -> float | None:
        #ricostruzione offline: media dei campioni osservati fra start ed end, filtrando
        #per zona se specificata. Ritorna None se non ci sono campioni
        query = (
            "SELECT AVG(g_per_kwh), COUNT(*) FROM grid_intensity_samples "
            "WHERE timestamp > ? AND timestamp < ?"
        )
        params: list = [to_utc_iso(start), to_utc_iso(end)]
        if zone is not None:
            query += " AND zone = ?"
            params.append(zone)
        with closing(self._connect()) as conn:
            row = conn.execute(query, params).fetchone()
        return row[0] if row[1] > 0 else None

    def recent_grid_intensities(self, limit: int = 288, zone: str | None = None) -> list[float]:
        #Ritorna gli ultimi campioni di intensita' osservati, ordinati 
        # dal piu' vecchio al piu' recente. limit e' il numero massimo 
        #di campioni da restituire, zone filtra per zona se specificata
        query = "SELECT g_per_kwh FROM grid_intensity_samples"
        params: list = []
        if zone is not None:
            query += " WHERE zone = ?"
            params.append(zone)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [row[0] for row in reversed(rows)]

    # -- aggregati giornalieri ---------------------------------------------

    def upsert_daily_summary(
        self,
        day: date,
        energy_wh: float,
        operational_gco2: float,
        active_hours: float,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO daily_summary (day, energy_wh, operational_gco2, active_hours) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET energy_wh=excluded.energy_wh, "
                "operational_gco2=excluded.operational_gco2, "
                "active_hours=excluded.active_hours",
                (day.isoformat(), energy_wh, operational_gco2, active_hours),
            )
            conn.commit()

