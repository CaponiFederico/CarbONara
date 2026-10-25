import logging
import random
from collections import deque
from datetime import date, datetime, timedelta

from ecomonitor.advisor import (
    AdviceTarget,
    CarbonAwareAdvisor,
    ChargeAdvice,
    ChargeRecommendation,
    RollingAverage,
)
from ecomonitor.carbon.attribution import CarbonAttributionService
from ecomonitor.carbon.backfill import backfill_grid_history
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
from ecomonitor.sensors.base import (
    NATIVE_MODE,
    PowerReading,
    SensorBackend,
    SensorLostError,
    now_utc,
)
from ecomonitor.sensors.factory import get_backend, get_fallback_backend
from ecomonitor.storage.db import LIVE_SAMPLE_SOURCE, LocalDatabase, default_database_path
from ecomonitor.storage.event_buffer import BufferedEnergyRow, EnergyEventBuffer, EnergyTick
from ecomonitor.study_phase import StudyPhase, hour_phase_label

#legge i sensori e integra W -> Wh, legge lo stato di
#alimentazione, campiona l'intensita' di rete con la sua cadenza, delega
#l'attribuzione, persiste consumo, eventuale ricarica e stato del pool.
GRID_BACKFILL_DAYS = 30

#Scarto casuale sull'intervallo di campionamento della rete
GRID_SAMPLE_JITTER_RATIO = 0.1

#Non importa Qt: la catena sensore -> carbonio -> database resta verificabile
#senza GUI, senza hardware e senza rete

logger = logging.getLogger(__name__)


def load_rolling_average(db: LocalDatabase, zone: str) -> RollingAverage:
    #La media di riferimento del consiglio
    rolling = RollingAverage()
    for value in db.recent_grid_intensities(
        rolling.window_size, zone=zone, sources=(LIVE_SAMPLE_SOURCE,)
    ):
        rolling.add(value)
    return rolling


POOL_SAVE_INTERVAL_SECONDS = 60.0

TOTALS_RESYNC_INTERVAL_SECONDS = 300.0
#ogni quanto rileggere i totali dal database invece di fidarsi del contatore

GAP_THRESHOLD_SECONDS = 600.0
#oltre questa distanza fra due tick l'app era chiusa o sospesa: un salto della
#percentuale e' una ricarica avvenuta al buio, da ricostruire

SINGLE_CHARGE_WINDOW_SECONDS = 24 * 3600.0
#  gap < GAP_THRESHOLD_SECONDS: dentro un ciclo si assume una  ricarica sola
#  la finestra viene stretta a [ultima osservazione, + carica% / velocita]

#  gap >= GAP_THRESHOLD_SECONDS: le ricariche possono essere piu', in istanti
#  ignoti, si media sull'intera finestra scoperta

#La media nazionale e' l'ultimo ripiego
#quando non esiste nessuno storico per quella finestra — non e' il
#comportamento normale sopra le 24 h.


POOL_STALE_AFTER_SECONDS = 7 * 24 * 3600.0
#Oltre una settimana senza osservare la batteria il pool e' considerato scaduto

def _pool_is_stale(last_seen: datetime | None, now: datetime) -> bool:
    if last_seen is None:
        return False
    return (now - last_seen).total_seconds() >= POOL_STALE_AFTER_SECONDS


class Orchestrator:
    def __init__(
        self,
        config_manager: ConfigManager | None = None,
        backend: SensorBackend | None = None,
        db: LocalDatabase | None = None,
        grid_service: GridCarbonIntensityService | None = None,
        power_source_reader=read_power_source_state,
        rng: random.Random | None = None,
    ) -> None:
        self._rng = rng or random.Random()
        self.config_manager = config_manager or ConfigManager()
        config = self.config_manager.config

        #il database precede il backend
        self.db = db or LocalDatabase(default_database_path())
        self.reporting = ReportingEngine(self.db)

        self.backend = backend or get_backend(self.db)
        #il fattore 12 lascia passare i tick in ritardo per un driver lento
        #senza lasciar passare un coperchio chiuso
        self.integrator = EnergyIntegrator(
            max_interval_seconds=max(30.0, config.poll_interval_seconds * 12)
        )
        self.buffer = EnergyEventBuffer(config.db_flush_interval_seconds)
        self._read_power_source = power_source_reader

        self.grid_service = grid_service or GridCarbonIntensityService(
            zone=config.em_zone,
            endpoint=config.effective_study_endpoint,
            country_code=config.country_code,
            local_history=self.db,
        )

        #il pool riparte da dove era rimasto: senza questo ogni riavvio
        #azzererebbe lo storico di ricarica
        pool, last_percent, last_seen = self.db.load_pool_state()
        if pool is not None and _pool_is_stale(last_seen, now_utc()):
            logger.info(
                "Storico di ricarica piu' vecchio di %.0f giorni: considerato scaduto e "
                "azzerato. Il consumo a batteria sara' attribuito alla media nazionale "
                "finche' non si osserva una nuova ricarica",
                POOL_STALE_AFTER_SECONDS / 86400.0,
            )
            pool, last_percent = None, None

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

        #Riferimento per l'energia gia' in batteria di cui non si conosce
        #l'istante di ricarica. Si parte dalla media nazionale e la si sostituisce
        #con la media della finestra scoperta quando c'e' e si riesce a leggerla
        self._unknown_origin_intensity = self.grid_service.static_country_mean
        self._unknown_origin_source = "static_country_mean"

        self.advisor = CarbonAwareAdvisor()
        self._last_advice: ChargeAdvice | None = None
        self.study_phase: StudyPhase | None = None
        self.dashboard_visible = False
        #solo i campioni della zona corrente: un portatile si sposta, e la
        #media di riferimento non puo' mescolare due mercati elettrici
        self.rolling_avg = load_rolling_average(self.db, self.grid_service.zone)

        #le ore attive ripartono da quelle gia' osservate oggi: sono il
        #denominatore di gCO2/ora, il cui numeratore copre l'intera giornata
        self._active_day = date.today()

        #stessa soglia dell'integratore
        self._active_seconds_today = self.db.active_seconds_for_day(
            self._active_day, self.integrator.max_interval_seconds
        )
        self._last_grid_sample_at: datetime | None = None
        #l'intervallo effettivo del prossimo campione, sorteggiato dopo ognuno
        self._grid_sample_due_seconds: float | None = None
        self._last_pool_save_at: datetime | None = None
        #placeholder, sostituito dal primo tick
        self._current_intensity = self.grid_service.static_country_mean

        self._written_wh = 0.0
        self._written_gco2 = 0.0
        #righe chiuse dal buffer ma non ancora sul database
        self._unwritten: deque[BufferedEnergyRow] = deque()
        self._last_totals_resync_at: datetime | None = None
        self._resync_totals(self._active_day)

    def apply_zone(self, zone: str, country_code: str | None = None) -> bool:
        #Cambia zona True se la zona e' cambiata
        config = self.config_manager.config
        config.em_zone = zone
        #anche il paese, nella configurazione in memoria: e' un ConfigManager
        #diverso da quello del dialogo, e un suo salvataggio successivo
        #riscriverebbe il file col paese vecchio
        if country_code:
            config.country_code = country_code
        if not self.grid_service.set_zone(zone, country_code):
            return False

        #il tick campiona subito
        self._last_grid_sample_at = None

        #la media di riferimento riparte
        self.rolling_avg = load_rolling_average(self.db, zone)
        #il riferimento per l'energia di origine ignota e' la media del paese:
        #se il paese e' cambiato, anche lui
        if self._unknown_origin_source == "static_country_mean":
            self._unknown_origin_intensity = self.grid_service.static_country_mean

        logger.info(
            "Zona ElectricityMaps aggiornata: %s, %d campioni gia' in archivio",
            zone,
            self.rolling_avg.sample_count,
        )
        return True

    def backfill_grid_history(self, days: int = GRID_BACKFILL_DAYS):
        #Colma le ore in cui l'app non girava con lo storico orario dell'API
        end = now_utc()
        return backfill_grid_history(self.db, self.grid_service, end - timedelta(days=days), end)

    @property
    def active_hours_today(self) -> float:
        return self._active_seconds_today / 3600.0

    # -- totali del giorno -------------------------------------------------

    def _write_energy_row(self, row: BufferedEnergyRow) -> None:
        self._unwritten.append(row)
        self._write_unwritten()

    def _write_unwritten(self) -> None:
        while self._unwritten:
            pending = self._unwritten[0]
            self.db.record_buffered_energy_row(pending)
            self._unwritten.popleft()
            if pending.timestamp.astimezone().date() == self._active_day:
                self._written_wh += pending.wh
                self._written_gco2 += pending.gco2

    def _resync_totals(self, day: date) -> None:
        totals = self.db.sum_energy_events_for_day(day)
        self._written_wh = totals["energy_wh"]
        self._written_gco2 = totals["operational_gco2"]

    def _daily_totals(self, now: datetime) -> tuple[float, float]:
        #Quanto e' gia' su database piu' quanto e' ancora nel buffer
        if (
            self._last_totals_resync_at is None
            or (now - self._last_totals_resync_at).total_seconds()
            >= TOTALS_RESYNC_INTERVAL_SECONDS
        ):
            self._resync_totals(self._active_day)
            self._last_totals_resync_at = now

        #le righe in coda non sono sul database ma sono energia di oggi
        unwritten = [
            row for row in self._unwritten if row.timestamp.astimezone().date() == self._active_day
        ]
        return (
            self._written_wh + self.buffer.pending_wh + sum(row.wh for row in unwritten),
            self._written_gco2 + self.buffer.pending_gco2 + sum(row.gco2 for row in unwritten),
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
            self._resync_totals(today)
            self._last_totals_resync_at = now

        
        self._active_seconds_today += self.integrator.last_interval_seconds

    def _refresh_grid_intensity(self, now: datetime) -> None:
        #si campiona sempre, collegati o no: alimenta la media mobile e lo
        #storico locale con cui si ricostruiscono le ricariche ad app chiusa
        interval = self.config_manager.config.grid_sample_interval_seconds
        due = self._grid_sample_due_seconds
        if due is None:
            due = interval
        if (
            self._last_grid_sample_at is not None
            and (now - self._last_grid_sample_at).total_seconds() < due
        ):
            return

        self._current_intensity = self.grid_service.get_current_intensity()
        self._last_grid_sample_at = now
        self._grid_sample_due_seconds = self._jittered_grid_interval(interval)
        source = self.grid_service.last_value_source
        #il riferimento del consiglio
        if source == LIVE_SAMPLE_SOURCE:
            self.rolling_avg.add(self._current_intensity)
        self.db.record_grid_intensity(
            now,
            self.grid_service.zone,
            self._current_intensity,
            source,
        )

    def _jittered_grid_interval(self, interval: float) -> float:
        #mai zero o negativo
        spread = interval * GRID_SAMPLE_JITTER_RATIO
        return max(1.0, interval + self._rng.uniform(-spread, spread))

    def _charge_window_end(
        self, start: datetime, now: datetime, charged_percent: float
    ) -> datetime:
        #Una batteria si ricarica in circa due ore e poi resta a fondo scala

        #Vale solo sotto SINGLE_CHARGE_WINDOW_SECONDS: e' il chiamante a decidere
        #se ancorare la finestra qui o prendere l'intera finestra scoperta
        rate = self.config_manager.config.charge_rate_percent_per_hour
        if rate <= 0 or charged_percent <= 0:
            return now
        estimated_end = start + timedelta(hours=charged_percent / rate)
        return min(now, estimated_end)

    def _resolve_charge_intensity(
        self, now: datetime, charged_percent: float
    ) -> tuple[float | None, bool, str]:
        #Per una ricarica avvenuta ad app chiusa si cerca 
        # di ricostruire la finestra di ricarica e la media di rete

        live_source = self.grid_service.last_value_source
        if self._last_seen_at is None:
            return None, False, live_source
        gap = (now - self._last_seen_at).total_seconds()
        if gap < GAP_THRESHOLD_SECONDS:
            return None, False, live_source
        if charged_percent <= MIN_SIGNIFICANT_DELTA_PERCENT:
            return None, False, live_source  # finestra scoperta ma nessuna ricarica

        uses_whole_window = gap > SINGLE_CHARGE_WINDOW_SECONDS
        if uses_whole_window:
            if self._unknown_origin_source == "static_country_mean":
                return self._no_history_for_charge(gap)
            return (
                self._unknown_origin_intensity,
                True,
                f"{self._unknown_origin_source}_window",
            )

        window_end = self._charge_window_end(self._last_seen_at, now, charged_percent)
        mean = self.grid_service.get_mean_intensity_between(self._last_seen_at, window_end)
        if mean is None:
            return self._no_history_for_charge(gap)
        return mean, True, self.grid_service.last_mean_source or "reconstructed_local"

    def _no_history_for_charge(self, gap: float) -> tuple[float, bool, str]:
        #La media nazionale non dipende dall'ora
        country_mean = self.grid_service.static_country_mean
        logger.info(
            "Ricarica avvenuta ad app chiusa (%.0f min) senza storico di rete: "
            "attribuita alla media nazionale (%.0f gCO2/kWh)",
            gap / 60.0,
            country_mean,
        )
        return country_mean, True, "static_country_mean"

    def _expire_pool(self, gap_seconds: float) -> None:
        #Il pool va svuotato 
        logger.info(
            "Batteria non osservata per %.1f giorni: storico di ricarica azzerato, "
            "si riparte dalla media nazionale",
            gap_seconds / 86400.0,
        )
        self.battery_pool.reset()
        #anche l'ultima percentuale e' scaduta: usarla darebbe un delta su una
        #settimana
        self._last_battery_percent = None
        self._unknown_origin_intensity = self.grid_service.static_country_mean
        self._unknown_origin_source = "static_country_mean"

    def _refresh_unknown_origin_reference(self, start: datetime, end: datetime) -> None:
        #Media sull'intera finestra scoperta: la carica non tracciata e' entrata
        #in un istante ignoto dentro la finestra
        mean = self.grid_service.get_mean_intensity_between(start, end)
        if mean is not None:
            self._unknown_origin_intensity = mean
            self._unknown_origin_source = self.grid_service.last_mean_source or "uncovered_local"
            return

        self._unknown_origin_intensity = self.grid_service.static_country_mean
        self._unknown_origin_source = "static_country_mean"
        logger.info(
            "Finestra scoperta di %.0f min senza storico di rete: l'energia di origine "
            "ignota sara' attribuita alla media nazionale (%.0f gCO2/kWh)",
            (end - start).total_seconds() / 60.0,
            self._unknown_origin_intensity,
        )

    def _handle_uncovered_window(self, now: datetime) -> None:
        #Va chiamato prima di calcolare il Delta% del tick: se il pool e' scaduto
        #azzera anche la percentuale di riferimento, e quel calcolo deve vederla
        if self._last_seen_at is None:
            return
        gap = (now - self._last_seen_at).total_seconds()
        if gap < GAP_THRESHOLD_SECONDS:
            return
        if gap >= POOL_STALE_AFTER_SECONDS:
            self._expire_pool(gap)
            return
        self._refresh_unknown_origin_reference(self._last_seen_at, now)

    def _read_sensor(self) -> PowerReading:
        try:
            return self.backend.read()
        except SensorLostError as exc:
            #senza cambio di backend ogni tick fallirebbe e le ore resterebbero vuote
            logger.warning(
                "Sensore nativo perso (%s): stimo il consumo fino al prossimo avvio", exc
            )
            close = getattr(self.backend, "close", None)
            if callable(close):
                close()
            self.backend = get_fallback_backend(self.db)
            return self.backend.read()

    def tick(self) -> dict:
        reading = self._read_sensor()
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
        self._handle_uncovered_window(now)

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
            unknown_origin_intensity_g_per_kwh=self._unknown_origin_intensity,
        )
        self._last_battery_percent = power_state.battery_percent
        self._last_seen_at = now

        interval = self.integrator.last_interval_seconds
        #dopo _read_sensor: un sensore perso cambia backend e livello
        measurement_mode = getattr(self.backend, "mode", NATIVE_MODE)
        previous = (
            None if self._last_advice is None else self._last_advice.recommendation
        )

        #gli eventi di consumo passano dal buffer, che chiude la riga da solo
        #quando cambia regime o dopo una finestra non osservata
        completed = self.buffer.add(
            EnergyTick(
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
                unknown_origin_intensity_g_per_kwh=self._unknown_origin_intensity,
                unknown_origin_source=self._unknown_origin_source,
                #registrato anche quando la potenza e' misurata davvero: e'
                #l'ascissa con cui si calibrera' il ripiego
                cpu_percent=reading.cpu_percent,
                #la durata che ha prodotto questi wh, per la riga aggregata
                interval_seconds=interval,
                #il solo dominio CPU: e' quello che il ripiego deve stimare
                #quando la GPU riesce a misurarla
                cpu_wh=self.integrator.last_package_wh,
                starts_new_row=self.integrator.skipped_gap_seconds > 0,
                study_phase=hour_phase_label(self.study_phase),
                measurement_kind=measurement_mode.kind,
                dashboard_visible_seconds=interval if self.dashboard_visible else 0.0,
                advice_wait_seconds=(
                    interval if previous is ChargeRecommendation.WAIT else 0.0
                ),
                advice_now_seconds=(
                    interval if previous is ChargeRecommendation.CHARGE_NOW else 0.0
                ),
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
            #a batteria o desktop
            target=AdviceTarget.WORKLOAD if power_state.is_plugged_in else AdviceTarget.CHARGE,
            now=now,
        )
        self._last_advice = advice

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
            #che cosa e' misurato davvero
            "measurement_mode": measurement_mode,
            "grid_intensity_source": self.grid_service.last_value_source,
            "grid_intensity_failure": self.grid_service.last_live_failure,
            "grid_zone": self.grid_service.zone,
            #da dove viene il riferimento applicato all'energia di origine
            #ignota
            "unknown_origin_source": self._unknown_origin_source,
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
        #Salvataggio finale
        try:
            pending = self.buffer.flush()
            if pending is not None:
                self._unwritten.append(pending)
            #anche le righe rimaste indietro da un errore precedente
            self._write_unwritten()
        except Exception:
            logger.exception(
                "Salvataggio finale non riuscito: %d intervalli di consumo non registrati",
                len(self._unwritten),
            )
        try:
            self.db.save_pool_state(
                self.battery_pool, self._last_battery_percent, self._last_seen_at or now_utc()
            )
        except Exception:
            logger.exception(
                "Salvataggio finale non riuscito: lo stato del pool batteria non e' stato "
                "salvato, alla riapertura una ricarica gia' contabilizzata potrebbe essere "
                "contata di nuovo"
            )

    def build_daily_report(self):
        return self.reporting.build_daily_report(date.today(), self.active_hours_today)
