import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import platformdirs

from ecomonitor.advisor import CarbonAwareAdvisor, ChargeAdvice, RollingAverage
from ecomonitor.carbon.attribution import CarbonAttributionService
from ecomonitor.carbon.battery_pool import (
    MIN_SIGNIFICANT_DELTA_PERCENT,
    AttributionModel,
    BatteryCarbonPool,
)
from ecomonitor.carbon.grid_intensity import GridCarbonIntensityService
from ecomonitor.config import ConfigManager
from ecomonitor.energy.integrator import EnergyIntegrator
from ecomonitor.power_context import PowerSourceState, read_power_source_state
from ecomonitor.reporting import ReportingEngine
from ecomonitor.sensors.base import SensorBackend, now_utc
from ecomonitor.sensors.factory import get_backend
from ecomonitor.storage.db import LocalDatabase
from ecomonitor.storage.event_buffer import EnergyEventBuffer, EnergySample

#Un tick, in ordine: legge i sensori e integra W -> Wh, legge lo stato di
#alimentazione, campiona l'intensita' di rete con la sua cadenza, delega
#l'attribuzione, persiste consumo, eventuale ricarica e stato del pool.
#Non importa Qt: la catena sensore -> carbonio -> database resta verificabile
#senza GUI, senza hardware e senza rete

logger = logging.getLogger(__name__)

POOL_SAVE_INTERVAL_SECONDS = 60.0

TOTALS_RESYNC_INTERVAL_SECONDS = 300.0
#ogni quanto rileggere i totali dal database invece di fidarsi del contatore

GAP_THRESHOLD_SECONDS = 600.0
#oltre questa distanza fra due tick l'app era chiusa o sospesa: un salto della
#percentuale e' una ricarica avvenuta al buio, da ricostruire


class CompensatedSum:
    #Somma con compensazione dell'errore (Kahan-Babuska-Neumaier). Serve perche'
    #il totale del giorno e' accumulato in memoria e non da SUM() di SQLite:
    #una somma ingenua diverge nell'ultimo bit dopo un centinaio di righe, e
    #l'errore cresce col numero di addendi

    __slots__ = ("_compensation", "_sum")

    def __init__(self, value: float = 0.0) -> None:
        self._sum = value
        self._compensation = 0.0

    def add(self, value: float) -> None:
        total = self._sum + value
        if abs(self._sum) >= abs(value):
            self._compensation += (self._sum - total) + value
        else:
            self._compensation += (value - total) + self._sum
        self._sum = total

    def reset(self, value: float = 0.0) -> None:
        self._sum = value
        self._compensation = 0.0

    @property
    def value(self) -> float:
        return self._sum + self._compensation


class Orchestrator:
    def __init__(
        self,
        config_manager: ConfigManager | None = None,
        backend: SensorBackend | None = None,
        db: LocalDatabase | None = None,
        grid_service: GridCarbonIntensityService | None = None,
        power_source_reader=read_power_source_state,
    ) -> None:
        self.config_manager = config_manager or ConfigManager()
        config = self.config_manager.config

        self.backend = backend or get_backend()
        #il fattore 12 lascia passare i tick in ritardo per un driver lento
        #senza lasciar passare un coperchio chiuso
        self.integrator = EnergyIntegrator(
            max_interval_seconds=max(30.0, config.poll_interval_seconds * 12)
        )
        self.buffer = EnergyEventBuffer(config.db_flush_interval_seconds)
        self._read_power_source = power_source_reader

        self.db = db or LocalDatabase(Path(platformdirs.user_data_path("ecomonitor")) / "local.db")
        self.reporting = ReportingEngine(self.db)

        self.grid_service = grid_service or GridCarbonIntensityService(
            zone=config.em_zone,
            api_key=config.api_key,
            country_code=config.country_code,
            local_history=self.db,
        )

        #il pool riparte da dove era rimasto: senza questo ogni riavvio
        #azzererebbe lo storico di ricarica
        pool, last_percent, last_seen = self.db.load_pool_state()
        configured_model = AttributionModel(config.attribution_model)
        if pool is None:
            pool = BatteryCarbonPool(model=configured_model)
        elif pool.switch_model(configured_model):
            #il modello arriva serializzato col pool: senza conversione,
            #cambiarlo in configurazione non avrebbe effetto dopo la prima
            #esecuzione, in silenzio
            logger.info(
                "Modello di attribuzione cambiato in '%s' da configurazione; "
                "la carica gia' tracciata e' stata convertita",
                configured_model.value,
            )
        self.battery_pool = pool
        self.attribution = CarbonAttributionService(pool)
        self._last_battery_percent = last_percent
        self._last_seen_at = last_seen

        self.advisor = CarbonAwareAdvisor()
        self.rolling_avg = RollingAverage()
        #solo i campioni della zona corrente: un portatile si sposta, e la
        #media di riferimento non puo' mescolare due mercati elettrici
        for value in self.db.recent_grid_intensities(
            self.rolling_avg.window_size, zone=self.grid_service.zone
        ):
            self.rolling_avg.add(value)

        #le ore attive ripartono da quelle gia' osservate oggi: sono il
        #denominatore di gCO2/ora, il cui numeratore copre l'intera giornata
        self._active_day = date.today()
        self._active_seconds_today = self.db.active_seconds_for_day(
            self._active_day, GAP_THRESHOLD_SECONDS
        )
        self._last_tick: datetime | None = None
        self._last_grid_sample_at: datetime | None = None
        self._last_pool_save_at: datetime | None = None
        self._current_intensity = self.grid_service.get_current_intensity()

        self._written_wh = CompensatedSum()
        self._written_gco2 = CompensatedSum()
        self._last_totals_resync_at: datetime | None = None
        self._resync_totals(self._active_day)

    @property
    def active_hours_today(self) -> float:
        return self._active_seconds_today / 3600.0

    # -- totali del giorno -------------------------------------------------

    def _write_energy_row(self, row) -> None:
        #unico punto in cui una riga finisce sul database, cosi' il totale in
        #memoria non puo' divergere per una scrittura dimenticata
        self.db.record_buffered_energy_row(row)
        if row.timestamp.astimezone().date() != self._active_day:
            return
        self._written_wh.add(row.wh)
        self._written_gco2.add(row.gco2)

    def _resync_totals(self, day: date) -> None:
        totals = self.db.sum_energy_events_for_day(day)
        self._written_wh.reset(totals["energy_wh"])
        self._written_gco2.reset(totals["operational_gco2"])

    def _daily_totals(self, now: datetime) -> tuple[float, float]:
        #Quanto e' gia' su database piu' quanto e' ancora nel buffer. La query
        #per tick costava ~3,5 ms di CPU, la voce piu' cara del ciclo di
        #polling in un'app che misura consumi: il totale e' esattamente cio'
        #che l'app ha appena scritto. Il risincrono periodico rende
        #l'ottimizzazione verificabile invece che creduta
        if (
            self._last_totals_resync_at is None
            or (now - self._last_totals_resync_at).total_seconds()
            >= TOTALS_RESYNC_INTERVAL_SECONDS
        ):
            self._resync_totals(self._active_day)
            self._last_totals_resync_at = now

        return (
            self._written_wh.value + self.buffer.pending_wh,
            self._written_gco2.value + self.buffer.pending_gco2,
        )

    def _accumulate_active_time(self, now: datetime) -> None:
        today = date.today()
        if today != self._active_day:
            #rollover di mezzanotte: il buffer va chiuso qui, altrimenti una
            #riga a cavallo finirebbe tutta sul giorno dopo
            pending = self.buffer.flush()
            if pending is not None:
                #la riga chiusa qui appartiene ancora al giorno precedente
                self._write_energy_row(pending)
            self._active_day = today
            self._active_seconds_today = 0.0
            self._last_tick = None
            self._resync_totals(today)
            self._last_totals_resync_at = now

        if self._last_tick is not None:
            elapsed = (now - self._last_tick).total_seconds()
            if 0 < elapsed < GAP_THRESHOLD_SECONDS:
                self._active_seconds_today += elapsed
        self._last_tick = now

    def _refresh_grid_intensity(self, now: datetime) -> None:
        #si campiona sempre, collegati o no: alimenta la media mobile e lo
        #storico locale con cui si ricostruiscono le ricariche ad app chiusa
        interval = self.config_manager.config.grid_sample_interval_seconds
        if (
            self._last_grid_sample_at is not None
            and (now - self._last_grid_sample_at).total_seconds() < interval
        ):
            return

        self._current_intensity = self.grid_service.get_current_intensity()
        self._last_grid_sample_at = now
        self.rolling_avg.add(self._current_intensity)
        self.db.record_grid_intensity(
            now,
            self.grid_service.zone,
            self._current_intensity,
            #la provenienza reale del valore, non la sola presenza di una
            #chiave: registrare come "live" una chiamata fallita renderebbe i
            #campioni non ricalcolabili
            self.grid_service.last_value_source,
        )

    def _charge_window_end(
        self, start: datetime, now: datetime, charged_percent: float
    ) -> datetime:
        #Una batteria si ricarica in circa due ore e poi resta a fondo scala:
        #mediare l'intensita' su tutta la notte attribuirebbe alla ricarica
        #anche sei ore in cui non caricava. La finestra viene ristretta alla
        #durata plausibile, ancorata all'inizio del periodo scoperto. Resta
        #un'assunzione dichiarata, non una misura
        rate = self.config_manager.config.charge_rate_percent_per_hour
        if rate <= 0 or charged_percent <= 0:
            return now
        estimated_end = start + timedelta(hours=charged_percent / rate)
        return min(now, estimated_end)

    def _resolve_charge_intensity(
        self, now: datetime, charged_percent: float
    ) -> tuple[float | None, bool, str]:
        #Per una ricarica avvenuta ad app chiusa ritorna la media della finestra
        #non osservata (e True), altrimenti (None, False) e si usa l'intensita'
        #corrente. Il terzo elemento e' la provenienza del numero
        live_source = self.grid_service.last_value_source
        if self._last_seen_at is None:
            return None, False, live_source
        gap = (now - self._last_seen_at).total_seconds()
        if gap < GAP_THRESHOLD_SECONDS:
            return None, False, live_source
        if charged_percent <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return None, False, live_source  # finestra scoperta ma nessuna ricarica

        window_end = self._charge_window_end(self._last_seen_at, now, charged_percent)
        mean = self.grid_service.get_mean_intensity_between(self._last_seen_at, window_end)
        if mean is None:
            logger.info(
                "Ricarica avvenuta ad app chiusa (%.0f min) senza storico di rete: "
                "attribuita all'intensita' corrente",
                gap / 60.0,
            )
            #ricostruita nel senso che il periodo non e' stato osservato, ma il
            #numero e' quello di adesso: vanno dette entrambe le cose
            return None, True, live_source
        return mean, True, self.grid_service.last_mean_source or "reconstructed_local"

    def tick(self) -> dict:
        reading = self.backend.read()
        delta_wh = self.integrator.add_reading(reading)
        power_state: PowerSourceState = self._read_power_source()
        now = reading.timestamp

        if self.integrator.skipped_gap_seconds > 0:
            logger.info(
                "Intervallo di %.0f min scartato (sospensione o app non in esecuzione): "
                "la finestra e' esclusa dal conteggio, non stimata",
                self.integrator.skipped_gap_seconds / 60.0,
            )

        self._accumulate_active_time(now)
        self._refresh_grid_intensity(now)

        charged_percent = 0.0
        if power_state.battery_percent is not None and self._last_battery_percent is not None:
            charged_percent = max(0.0, power_state.battery_percent - self._last_battery_percent)

        charge_intensity, is_reconstructed, intensity_source = self._resolve_charge_intensity(
            now, charged_percent
        )
        result = self.attribution.attribute(
            delta_wh=delta_wh,
            is_plugged_in=power_state.is_plugged_in,
            battery_percent=power_state.battery_percent,
            previous_battery_percent=self._last_battery_percent,
            grid_intensity_g_per_kwh=self._current_intensity,
            charge_intensity_g_per_kwh=charge_intensity,
        )

        #gli eventi di consumo passano dal buffer, che chiude la riga da solo
        #quando cambia regime o dopo una finestra non osservata
        completed = self.buffer.add(
            EnergySample(
                timestamp=now,
                wh=delta_wh,
                gco2=result.gco2,
                intensity_g_per_kwh=result.intensity_g_per_kwh,
                is_plugged=power_state.is_plugged_in,
                is_estimated=reading.is_estimated,
                attribution_source=result.source.value,
                battery_percent=power_state.battery_percent,
                charged_percent=result.charged_percent,
                discharged_percent=result.discharged_percent,
                pool_coverage_ratio=result.pool_coverage_ratio,
                battery_assisted=result.battery_assisted,
                starts_new_row=self.integrator.skipped_gap_seconds > 0,
            )
        )
        if completed is not None:
            self._write_energy_row(completed)

        #le ricariche NON passano dal buffer: sono ~100 righe al giorno e ognuna
        #porta l'intensita' esatta del suo punto percentuale
        if result.charged_percent > MIN_SIGNIFICANT_DELTA_PERCENT:
            self.db.record_charge_event(
                now,
                result.charged_percent,
                charge_intensity if charge_intensity is not None else self._current_intensity,
                is_reconstructed=is_reconstructed,
                intensity_source=intensity_source,
            )

        self._last_battery_percent = power_state.battery_percent
        self._last_seen_at = now
        battery_moved = (
            result.charged_percent > MIN_SIGNIFICANT_DELTA_PERCENT
            or result.discharged_percent > MIN_SIGNIFICANT_DELTA_PERCENT
        )
        self._maybe_save_pool_state(now, force=battery_moved)

        advice: ChargeAdvice = self.advisor.advise(
            current_intensity=self._current_intensity,
            forecast=self.grid_service.get_forecast(),
            reference_intensity=self.rolling_avg.average,
            battery_percent=power_state.battery_percent,
            has_battery=power_state.battery_percent is not None,
            now=now,
        )

        #i totali viaggiano col risultato del tick: la finestra non deve fare
        #I/O SQLite per conto suo
        today_wh, today_gco2 = self._daily_totals(now)
        return {
            "reading": reading,
            "delta_wh": delta_wh,
            "gco2": result.gco2,
            "attribution": result,
            "power_state": power_state,
            "advice": advice,
            "today_energy_wh": today_wh,
            "today_operational_gco2": today_gco2,
            "active_hours_today": self.active_hours_today,
            "grid_intensity_source": self.grid_service.last_value_source,
            "grid_zone": self.grid_service.zone,
        }

    def _maybe_save_pool_state(self, now: datetime, force: bool = False) -> None:
        #force=True quando la percentuale si e' appena mossa, e non e' una
        #ottimizzazione: last_battery_percent e' il riferimento con cui alla
        #riapertura si calcola il primo Delta%. Salvandolo solo ogni 60 s, una
        #terminazione non pulita lascia su disco un valore vecchio e l'istanza
        #successiva registra di nuovo una ricarica gia' contabilizzata
        #(osservato: 62 punti in charge_events contro 61 salite reali)
        if (
            not force
            and self._last_pool_save_at is not None
            and (now - self._last_pool_save_at).total_seconds() < POOL_SAVE_INTERVAL_SECONDS
        ):
            return
        self.db.save_pool_state(self.battery_pool, self._last_battery_percent, now)
        self._last_pool_save_at = now

    def flush(self) -> None:
        #salvataggio finale: salva l'istante dell'ultima osservazione reale
        #della batteria, non l'ora di adesso, perche' e' il valore con cui si
        #misura la finestra rimasta scoperta
        pending = self.buffer.flush()
        if pending is not None:
            self._write_energy_row(pending)
        self.db.save_pool_state(
            self.battery_pool, self._last_battery_percent, self._last_seen_at or now_utc()
        )

    def build_daily_report(self):
        return self.reporting.build_daily_report(date.today(), self.active_hours_today)
