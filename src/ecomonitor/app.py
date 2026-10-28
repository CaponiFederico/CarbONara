from __future__ import annotations

import logging
import os
import platform
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import platformdirs
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory, QSystemTrayIcon

from ecomonitor import autostart
from ecomonitor.config import AppConfig, ConfigManager
from ecomonitor.gui import macos_dock
from ecomonitor.gui.icon import app_icon
from ecomonitor.gui.main_window import MainWindow
from ecomonitor.gui.notifier import Notifier, create_notifier
from ecomonitor.gui.single_instance import SingleInstanceGuard
from ecomonitor.gui.theme import theme_for
from ecomonitor.gui.tray import create_tray_icon, set_study_baseline
from ecomonitor.storage.db import default_database_path
from ecomonitor.storage.lock import DatabaseLock
from ecomonitor.study_phase import StudyDates, StudyPhase, new_install_id, parse_moment

if TYPE_CHECKING:
    #a runtime li importa il thread di polling
    from ecomonitor.advisor import ChargeAdvice
    from ecomonitor.notifications import Notification, NotificationPolicy
    from ecomonitor.orchestrator import Orchestrator
    from ecomonitor.study_upload import StudyUploadScheduler, UploadSettings

#Orchestrator.tick() fa I/O bloccante verso i driver:
#sul thread della GUI l'event loop di Qt si blocca e la finestra smette di
#rispondere. PollingWorker lo sposta su un QThread dedicato e comunica solo via
#segnali
#
#Anche costruire l'Orchestrator e' I/O bloccante, e lo e' molto di piu' di un
#tick: apre i sensori e il database
#Per questo la
#costruzione sta dentro run(), cioe' sul thread di polling, e non in main()

logger = logging.getLogger(__name__)

#Stili
_DARK_AWARE_STYLES = ("windows11", "Fusion")
PLATFORM_THEME_ENV_VAR = "QT_QPA_PLATFORMTHEME"


def choose_dark_aware_style(
    scheme_is_dark: bool, window_lightness: int, available: tuple[str, ...]
) -> str | None:
    #Se il sistema e' in tema scuro ma lo stile attuale rende chiaro, sceglie un
    #altro stile che rispetti il tema scuro. Se non c'e', non fa nulla: 
    #il tema scuro e' solo un suggerimento, non un obbligo
    if not scheme_is_dark:
        return None
    if window_lightness < 128:
        return None  # lo stile attuale sta gia' scurendo per conto suo
    for name in _DARK_AWARE_STYLES:
        if name in available:
            return name
    return None


def background_mode_available(system: str, tray_available: bool, can_be_restored: bool) -> bool:
    #Su GNOME non esiste area di notifica
    if tray_available:
        return False  # con la tray l'app e' sempre richiamabile, non serve un secondo avvio
    if system != "Linux":
        return False
    return can_be_restored


def hidden_start_requested(arguments: Sequence[str]) -> bool:
    #lo passa la voce di avvio automatico
    return autostart.HIDDEN_START_FLAG in arguments


def should_start_hidden(requested: bool, tray_available: bool, background_available: bool) -> bool:
    if not requested:
        return False
    return tray_available or background_available


def needs_hand_over(system: str, frozen: bool, elevated: bool) -> bool:
    return system == "Windows" and frozen and not elevated


def hand_over_to_elevated_instance(
    arguments: Sequence[str],
    *,
    request_window: Callable[[], bool],
    launcher_ready: Callable[[], bool],
    run_launcher: Callable[[], bool],
    relaunch_as_administrator: Callable[[list[str]], bool],
) -> bool:
    if request_window():
        logger.info("EcoMonitor e' gia' in esecuzione: mostrata la sua finestra")
        return True
    if launcher_ready() and run_launcher():
        logger.info("Avvio passato all'attivita' di lancio, elevata senza UAC")
        return True
    #installazione non finita: meglio la UAC di sempre che una CPU stimata
    logger.warning("Attivita' di lancio assente o non avviata: si chiede la conferma di Windows")
    if relaunch_as_administrator(list(arguments)):
        return True
    logger.warning("Elevazione rifiutata: EcoMonitor parte senza privilegi e stima la CPU")
    return False


def needs_onboarding(frozen: bool, config: AppConfig) -> bool:
    return frozen and not config.study_onboarding_done and not config.study_token


def starts_hidden_for_study(
    requested: bool,
    phase: StudyPhase | None,
    onboarding: bool,
    tray_available: bool,
    background_available: bool,
) -> bool:
    if onboarding:
        return False
    return should_start_hidden(
        requested or phase is StudyPhase.BASELINE, tray_available, background_available
    )


#open --args --background attiva l'app all'avvio
HIDDEN_START_ACTIVATION_GRACE_SECONDS = 3.0

HIDE_ACTIVATION_GRACE_SECONDS = 1.0


def reopens_on_activation(
    system: str,
    window_visible: bool,
    started_hidden: bool,
    seconds_since_start: float,
    seconds_since_hidden: float | None,
) -> bool:
    #Dock, Finder e Spotlight non lanciano una seconda istanza su macOS:
    #riattivano quella in esecuzione
    if system != "Darwin" or window_visible:
        return False
    if started_hidden and seconds_since_start < HIDDEN_START_ACTIVATION_GRACE_SECONDS:
        return False
    return seconds_since_hidden is None or seconds_since_hidden >= HIDE_ACTIVATION_GRACE_SECONDS

BACKFILL_RETRY_SECONDS = 15 * 60.0
TRAY_WAIT_SECONDS = 30.0
TRAY_WAIT_STEP_SECONDS = 0.25

def waits_for_tray(requested_hidden: bool, system: str, tray_available: bool) -> bool:
    return requested_hidden and system == "Windows" and not tray_available

def wait_for_tray(
    is_available: Callable[[], bool],
    now: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float = TRAY_WAIT_SECONDS,
    step: float = TRAY_WAIT_STEP_SECONDS,
) -> bool:
    #Torna True appena l'area di notifica compare, False allo scadere
    deadline = now() + timeout
    while True:
        if is_available():
            return True
        if now() >= deadline:
            return False
        sleep(step)

#Testo dell'avviso
TRAY_NOTICE_TITLE = "EcoMonitor continua a misurare"
TRAY_NOTICE_TEXT = (
    "La finestra è nascosta, l'app resta attiva nella barra delle "
    "applicazioni. Per uscire davvero: clic destro sull'icona → Esci."
)
BACKGROUND_NOTICE_TITLE = "EcoMonitor continua a misurare"
BACKGROUND_NOTICE_TEXT = (
    "La finestra è stata chiusa ma l'applicazione resta attiva e continua "
    "a misurare.\n\nPer rivederla, riavvia EcoMonitor dal menu delle "
    "applicazioni: tornerà in primo piano.\n\nPer fermare davvero la "
    "misurazione, usa il pulsante «Esci» in fondo alla finestra."
)

DATABASE_BUSY_TITLE = "EcoMonitor è già in esecuzione"


def database_busy_text(system: str) -> str:
    text = (
        "Un'altra copia di EcoMonitor sta già misurando su questo computer, quindi "
        "questa si chiude senza toccare i dati."
    )
    if system == "Windows":
        #la voce «(amministratore)» nel menu Start non c'e' piu': l'eseguibile
        #impacchettato chiede sempre l'elevazione
        text += (
            "\n\nSe è partita all'accesso, riaprila dall'icona nell'area di "
            "notifica."
        )
    return text


NoticeChannel = Literal["tray", "dialog", "none"]


def background_notice_channel(
    tray_available: bool,
    tray_supports_messages: bool,
    background_available: bool,
    tray_notice_shown: bool,
    dialog_notice_shown: bool,
) -> NoticeChannel:
    #Chiudere la finestra non ferma la misurazione
    if tray_available:
        if tray_notice_shown or not tray_supports_messages:
            return "none"
        return "tray"
    if not background_available:
        return "none"  # chiudere termina davvero: non c'e' niente da avvertire
    if dialog_notice_shown:
        return "none"
    return "dialog"


def build_background_notice() -> QMessageBox:
    notice = QMessageBox(
        QMessageBox.Icon.Information,
        BACKGROUND_NOTICE_TITLE,
        BACKGROUND_NOTICE_TEXT,
        QMessageBox.StandardButton.Ok,
    )
    for button in notice.buttons():
        button.setIcon(QIcon())
    notice.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    #QMessageBox 
    notice.setWindowModality(Qt.WindowModality.NonModal)
    return notice


class BackgroundNoticePresenter:
    def __init__(self) -> None:
        self._open: list[QMessageBox] = []

    @property
    def is_showing(self) -> bool:
        return bool(self._open)

    def show(self) -> QMessageBox:
        notice = build_background_notice()
        notice.finished.connect(lambda _: self._open.clear())
        self._open.append(notice)
        notice.show()
        return notice


DefaultAutostartAction = Literal["enable", "record", "wait", "skip"]


def default_autostart_action(
    supported: bool,
    already_applied: bool,
    already_enabled: bool,
    needs_elevation: bool,
    declined_by_installer: bool = False,
) -> DefaultAutostartAction:
    #L'avvio automatico nasce attivo, ma l'utente puo' disattivarlo
    if not supported or already_applied:
        return "skip"
    if declined_by_installer:
        return "record"
    if already_enabled:
        #gia' attivo
        return "record"
    if needs_elevation:
        return "wait"
    return "enable"


def run_default_autostart(config: AppConfig) -> bool:
    supported = autostart.is_supported()
    already_applied = config.autostart_default_applied
    decided = not supported or already_applied
    action = default_autostart_action(
        supported=supported,
        already_applied=already_applied,
        already_enabled=False if decided else autostart.is_enabled(),
        needs_elevation=False if decided else autostart.needs_elevation(),
        declined_by_installer=False if decided else autostart.installer_declined(),
    )
    if action in ("skip", "wait"):
        return False

    if action == "enable":
        try:
            autostart.set_enabled(True)
        except autostart.AutostartError as exc:
            logger.warning("Non riesco ad attivare l'avvio automatico: %s", exc)
            return False
        logger.info(
            "Avvio automatico attivato: EcoMonitor si aprira' all'accesso, in primo "
            "piano. Si disattiva dalle impostazioni"
        )
    return True

def apply_default_autostart(config_manager: ConfigManager) -> None:
    if run_default_autostart(config_manager.config):
        record_default_autostart(config_manager)


class DefaultAutostartRunner(QObject):
    recorded = Signal()

    def __init__(self, config_manager: ConfigManager) -> None:
        super().__init__()
        self._config_manager = config_manager
        self.recorded.connect(self._record)

    def start(self) -> None:
        #chi lo crea deve tenerlo vivo fino alla fine
        config = self._config_manager.config
        threading.Thread(
            target=self._run, args=(config,), name="default-autostart", daemon=True
        ).start()

    def _run(self, config: AppConfig) -> None:
        try:
            if run_default_autostart(config):
                self.recorded.emit()
        except RuntimeError:
            #applicazione gia' chiusa: si riprova al prossimo avvio
            logger.debug("Avvio automatico predefinito concluso ad applicazione chiusa")
        except Exception:
            logger.exception("Avvio automatico predefinito non applicato")

    @Slot()
    def _record(self) -> None:
        record_default_autostart(self._config_manager)


def record_default_autostart(config_manager: ConfigManager) -> None:
    config = config_manager.config
    config.autostart_default_applied = True
    try:
        config_manager.save()
    except OSError as exc:
        logger.warning(
            "Avvio automatico attivato ma non riesco a ricordarlo (%s): "
            "spegnerlo dalle impostazioni potrebbe non bastare",
            exc,
        )


def platform_theme_to_force(system: str, current: str | None, plugin_exists: bool) -> str | None:
    if system != "Linux":
        return None
    if current is not None:
        return None
    if not plugin_exists:
        return None
    return "xdgdesktopportal"


def _xdg_portal_plugin_exists() -> bool:
    try:
        import PySide6
    except ImportError:
        return False
    plugins = Path(PySide6.__file__).parent / "Qt" / "plugins" / "platformthemes"
    return (plugins / "libqxdgdesktopportal.so").exists()


def _force_platform_theme() -> None:
    chosen = platform_theme_to_force(
        system=platform.system(),
        current=os.environ.get(PLATFORM_THEME_ENV_VAR),
        plugin_exists=_xdg_portal_plugin_exists(),
    )
    if chosen is not None:
        os.environ[PLATFORM_THEME_ENV_VAR] = chosen


def _system_prefers_dark(app: QApplication) -> bool:
    #La preferenza dichiarata dal sistema
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _apply_dark_aware_style(app: QApplication) -> None:
    palette_window = app.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window)
    chosen = choose_dark_aware_style(
        scheme_is_dark=_system_prefers_dark(app),
        window_lightness=palette_window.lightness(),
        available=tuple(QStyleFactory.keys()),
    )
    if chosen is None:
        return
    previous = app.style().objectName()
    app.setStyle(chosen)
    app.setPalette(app.style().standardPalette())
    window = app.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window)
    available = QStyleFactory.keys()
    if window.lightness() >= 128 and chosen != "Fusion" and "Fusion" in available:
        chosen = "Fusion"
        app.setStyle(chosen)
        app.setPalette(app.style().standardPalette())
    logger.info(
        "Sistema in tema scuro ma lo stile '%s' rende chiaro: passo a '%s'", previous, chosen
    )


def _build_orchestrator(config_manager: ConfigManager) -> Orchestrator:
    #l'import sta qui e non in cima: tira dentro httpx e i sensori, che in cima
    #si pagherebbero prima che la finestra compaia
    from ecomonitor.orchestrator import Orchestrator

    return Orchestrator(config_manager=config_manager)


class PollingWorker(QObject):
    #costruisce l'Orchestrator ed esegue tick() fuori dal thread della GUI

    tick_ready = Signal(dict)
    startup_failed = Signal(str)

    def __init__(
        self,
        config_manager: ConfigManager,
        poll_interval_seconds: float,
        orchestrator_factory: Callable[..., Orchestrator] = _build_orchestrator,
    ) -> None:
        super().__init__()
        #la configurazione arriva gia' letta dal thread della GUI
        self._config_manager = config_manager
        self._orchestrator_factory = orchestrator_factory
        self._orchestrator: Orchestrator | None = None
        self._poll_interval_ms = int(poll_interval_seconds * 1000)
        self._running = True

        #Richiesta di cambio zona o token
        self._pending_lock = threading.Lock()
        self._pending_zone: tuple[str, str | None] | None = None
        #(token, calendario ricevuto dalla verifica), oppure () se non c'e' richiesta
        self._pending_study_token: tuple[()] | tuple[str | None, StudyDates | None] = ()
        #la dashboard a schermo, scritta dal thread della GUI
        self._dashboard_visible = False

        #invio allo studio: nasce col primo tick, gira in un thread suo
        self._study_scheduler: StudyUploadScheduler | None = None
        #quando riprovare un riempimento dello storico rimasto senza risposta
        self._backfill_retry_at: float | None = None
        self._install_id: str | None = None

    def _backfill_grid_history(self) -> None:
        #Un errore non deve fermare il monitoraggio
        self._backfill_retry_at = None
        if self._orchestrator is None:
            return
        try:
            result = self._orchestrator.backfill_grid_history()
        except Exception:
            logger.exception("Riempimento dello storico di rete non riuscito")
            return
        if result.should_retry:
            self._backfill_retry_at = time.monotonic() + BACKFILL_RETRY_SECONDS
            logger.info(
                "Storico di rete: %d richieste senza risposta, si riprova fra %.0f min",
                result.failed_requests,
                BACKFILL_RETRY_SECONDS / 60.0,
            )

    def _backfill_retry_due(self) -> bool:
        return self._backfill_retry_at is not None and time.monotonic() >= self._backfill_retry_at

    @property
    def has_orchestrator(self) -> bool:
        #letto dal thread della GUI alla chiusura
        return self._orchestrator is not None

    def request_zone(self, zone: str, country_code: str | None = None) -> None:
        #chiamata dal thread della GUI quando si sceglie una zona 
        with self._pending_lock:
            self._pending_zone = (zone, country_code)

    def _take_pending_zone(self) -> tuple[str, str | None] | None:
        with self._pending_lock:
            pending, self._pending_zone = self._pending_zone, None
        return pending

    def _apply_pending_zone(self) -> bool:
        #True se la zona e' cambiata
        pending = self._take_pending_zone()
        if pending is None or self._orchestrator is None:
            return False
        zone, country_code = pending
        try:
            return self._orchestrator.apply_zone(zone, country_code)
        except Exception:
            #una zona rifiutata non deve fermare la misura
            logger.exception("Zona ElectricityMaps non applicata")
            return False

    def request_study_token(self, token: str | None, dates: StudyDates | None = None) -> None:
        with self._pending_lock:
            self._pending_study_token = (token, dates)

    def _take_pending_study_token(self) -> tuple[()] | tuple[str | None, StudyDates | None]:
        with self._pending_lock:
            pending, self._pending_study_token = self._pending_study_token, ()
        return pending

    def _apply_pending_study_token(self) -> None:
        pending = self._take_pending_study_token()
        if not pending or self._orchestrator is None:
            return
        token, dates = pending
        config = self._orchestrator.config_manager.config
        if token != config.study_token:
            #un altro partecipante: si riparte dalla fase 1
            config.reset_study_calendar()
        config.study_token = token
        if dates is not None:
            config.apply_study_dates(dates)
        if self._study_scheduler is not None:
            self._study_scheduler.reset()

    def set_dashboard_visible(self, visible: bool) -> None:
        #chiamata dal thread della GUI a ogni cambio
        with self._pending_lock:
            self._dashboard_visible = visible

    def _sync_study_phase(self) -> dict:
        assert self._orchestrator is not None
        config = self._orchestrator.config_manager.config
        dates = None if self._study_scheduler is None else self._study_scheduler.study_dates
        if dates is not None and dates.advice_starts_at is not None:
            config.apply_study_dates(dates)
        now = datetime.now(UTC)
        phase = config.study_phase(now)
        with self._pending_lock:
            visible = self._dashboard_visible
        self._orchestrator.study_phase = config.recorded_study_phase(now)
        self._orchestrator.dashboard_visible = visible
        return {
            "study_phase": phase,
            "study_advice_starts_at": parse_moment(config.study_advice_starts_at),
            "study_ends_at": parse_moment(config.study_ends_at),
        }

    @staticmethod
    def _study_settings(config: AppConfig, install_id: str | None) -> UploadSettings | None:
        from ecomonitor.study_upload import UploadSettings, settle_seconds_for

        endpoint = config.effective_study_endpoint
        if not (endpoint and config.study_token and install_id):
            return None
        return UploadSettings(
            endpoint=endpoint,
            token=config.study_token,
            install_id=install_id,
            zone=config.em_zone,
            country_code=config.country_code,
            settle_seconds=settle_seconds_for(config.db_flush_interval_seconds),
        )

    def _upload_study_hours(self) -> None:
        if self._orchestrator is None:
            return
        try:
            if self._study_scheduler is None:
                from ecomonitor.study_upload import StudyUploadScheduler

                self._study_scheduler = StudyUploadScheduler(self._orchestrator.db)
            config = self._orchestrator.config_manager.config
            if self._install_id is None:
                self._install_id = self._orchestrator.db.install_id(
                    config.install_id or new_install_id()
                )
            self._study_scheduler.maybe_start(self._study_settings(config, self._install_id))
        except Exception:
            logger.exception("Invio allo studio non avviato")

    def _has_pending_request(self) -> bool:
        with self._pending_lock:
            return self._pending_zone is not None or bool(self._pending_study_token)

    @Slot()
    def run(self) -> None:
        try:
            self._orchestrator = self._orchestrator_factory(config_manager=self._config_manager)
        except Exception:
            #senza orchestratore si esce senza toccare il database
            logger.exception("Avvio del monitoraggio non riuscito")
            self.startup_failed.emit(
                "avvio non riuscito: nessuna misura in corso — vedi il registro"
            )
            return

        #dopo il primo tick
        first_tick_done = False

        while self._running:
            zone_changed = self._apply_pending_zone()
            self._apply_pending_study_token()
            try:
                study = self._sync_study_phase()
            except Exception:
                #un difetto qui non deve fermare la misura
                logger.exception("Fase dello studio non aggiornata")
                study = {}
            try:
                result = self._orchestrator.tick()
                #lo scheduler vive qui, non nell'orchestratore
                result["study_token_rejected"] = (
                    self._study_scheduler is not None and self._study_scheduler.token_rejected
                )
                result.update(study)
                self.tick_ready.emit(result)
                if not first_tick_done:
                    first_tick_done = True
                    self._backfill_grid_history()
                elif zone_changed or self._backfill_retry_due():
                    #dopo il tick
                    self._backfill_grid_history()
            except Exception:
                #un tick fallito (sensore momentaneamente non leggibile, disco
                #occupato) non deve bloccare l'app: si riprova al prossimo intervallo
                logger.exception("Tick fallito, si riprova al prossimo intervallo")

            self._upload_study_hours()
            self._interruptible_sleep(self._poll_interval_ms)

        #il salvataggio finale sta qui e non in cleanup()
        self._orchestrator.flush()

    def _interruptible_sleep(self, total_ms: int) -> None:
        #a passi piccoli cosi' stop() viene
        #notato entro ~100ms: altrimenti alla chiusura il thread resta
        #addormentato oltre il timeout di wait() e Qt aborta
        step_ms = 100
        elapsed = 0
        while elapsed < total_ms and self._running and not self._has_pending_request():
            QThread.msleep(min(step_ms, total_ms - elapsed))
            elapsed += step_ms

    def stop(self) -> None:
        self._running = False


class WindowUpdater(QObject):
    #Riceve i tick e aggiorna la finestra. Deve essere un QObject creato nel
    #thread della GUI, con lo slot registrato: solo allora Qt accoda la chiamata
    #all'event loop del thread giusto

    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self._window = window

    @Slot(dict)
    def on_tick_ready(self, result: dict) -> None:
        #le due tracce delimitano l'aggiornamento della finestra
        logger.debug("Aggiornamento finestra: inizio")
        try:
            self._apply_tick(result)
        except Exception:
            #senza questo la finestra resterebbe all'ultimo valore
            logger.exception("Aggiornamento della finestra non riuscito")
            return
        logger.debug("Aggiornamento finestra: fine")

    def _apply_tick(self, result: dict) -> None:
        reading = result["reading"]
        attribution = result["attribution"]
        power_state = result["power_state"]
        self._window.update_power(reading.total_w, reading.is_estimated)
        self._window.update_power_state(power_state.is_plugged_in, power_state.battery_percent)
        #i totali arrivano gia' calcolati dal thread di polling: la finestra
        #non deve interrogare SQLite per conto suo
        self._window.update_daily_totals(
            result["today_energy_wh"], result["today_operational_gco2"]
        )
        self._window.update_active_hours(result["active_hours_today"])
        self._window.update_attribution(
            attribution.intensity_g_per_kwh,
            attribution.source.value,
            attribution.is_estimated_intensity,
            grid_source=result["grid_intensity_source"],
            grid_failure=result["grid_intensity_failure"],
            zone=result["grid_zone"],
            coverage_ratio=attribution.pool_coverage_ratio,
        )

        advice: ChargeAdvice = result["advice"]
        #la raccomandazione viaggia accanto al testo: il messaggio da solo non
        #dice alla finestra di che colore dipingere il riquadro
        self._window.update_advisor(
            advice.headline(),
            advice.recommendation.value,
            advice.target.value,
            runners_up=advice.runners_up(),
            detail=advice.message(),
            outlook=advice.outlook,
            podium=advice.slots,
        )

        self._window.update_measurement_mode(result["measurement_mode"])
        self._window.fit_to_content()
        self._window.mark_updated()

    @Slot(str)
    def on_startup_failed(self, reason: str) -> None:
        self._window.mark_startup_failed(reason)


class NotificationDispatcher(QObject):
    #Riceve i tick sul thread della GUI e mostra le notifiche

    def __init__(
        self,
        window: MainWindow,
        config: AppConfig,
        notifier_factory: Callable[[], Notifier | None],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self._window = window
        self._config = config
        self._now = now
        self._started_at = now()
        self._notifier_factory = notifier_factory
        self._notifier: Notifier | None = None
        self._policy: NotificationPolicy | None = None

    def set_switches(self, advice_enabled: bool, problems_enabled: bool) -> None:
        self._config.advice_notifications_enabled = advice_enabled
        self._config.problem_notifications_enabled = problems_enabled

    def _window_in_foreground(self) -> bool:
        window = self._window
        return window.isVisible() and not window.isMinimized() and window.isActiveWindow()

    @Slot(dict)
    def on_tick_ready(self, result: dict) -> None:
        try:
            self._dispatch(result)
        except Exception:
            logger.exception("Notifica non riuscita")

    def _ensure_started(self) -> None:
        from ecomonitor.notifications import NotificationPolicy

        if self._policy is None:
            #prima la politica
            self._policy = NotificationPolicy(self._started_at)
            self._notifier = self._notifier_factory()

    def show_notice(self, notification: Notification) -> bool:
        try:
            self._ensure_started()
            if self._notifier is None:
                return False
            logger.info("Notifica: %s", notification.kind.value)
            self._notifier.show(notification)
        except Exception:
            logger.exception("Notifica non riuscita")
            return False
        return True

    def _dispatch(self, result: dict) -> None:
        from ecomonitor.notifications import advice_notifications_allowed, snapshot_from_tick

        self._ensure_started()
        if self._notifier is None or self._policy is None:
            return
        config = self._config
        phase = (
            result["study_phase"]
            if "study_phase" in result
            else config.study_phase(self._now())
        )
        snapshot = snapshot_from_tick(
            result,
            window_in_foreground=self._window_in_foreground(),
            advice_enabled=advice_notifications_allowed(
                config.advice_notifications_enabled, phase
            ),
            problems_enabled=config.problem_notifications_enabled,
        )
        outcome = self._policy.evaluate(snapshot, self._now())
        for kind in outcome.withdraw:
            logger.info("Notifica ritirata: %s", kind.value)
            self._notifier.withdraw(kind)
        if outcome.show is not None:
            logger.info("Notifica: %s", outcome.show.kind.value)
            self._notifier.show(outcome.show)


def _calendar_fields(config: AppConfig) -> tuple[str | None, ...]:
    return (config.study_starts_at, config.study_advice_starts_at, config.study_ends_at)


class StudyPhaseController(QObject):

    def __init__(
        self,
        window: MainWindow,
        config_manager: ConfigManager,
        show_window: Callable[[], None],
        show_notice: Callable[[Notification], bool],
        on_baseline_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self._window = window
        self._config_manager = config_manager
        self._show_window = show_window
        self._show_notice = show_notice
        self._on_baseline_changed = on_baseline_changed
        config = config_manager.config
        self._saved_dates = _calendar_fields(config)
        self._in_baseline: bool | None = None

    @Slot(dict)
    def on_tick_ready(self, result: dict) -> None:
        #senza la chiave la fase non e' stata calcolata
        if "study_phase" not in result:
            return
        try:
            self._apply(result["study_phase"], result.get("study_advice_starts_at"))
        except Exception:
            logger.exception("Fase dello studio non applicata")

    def apply_phase(
        self, phase: StudyPhase | None, advice_starts_at: datetime | None = None
    ) -> None:
        #la stessa strada di un tick
        self._apply(phase, advice_starts_at)

    def _apply(self, phase: StudyPhase | None, advice_starts_at: datetime | None) -> None:
        config = self._config_manager.config
        #il thread di polling scrive il calendario nel config in memoria
        dates = _calendar_fields(config)
        if dates != self._saved_dates:
            self._saved_dates = dates
            self._save("il calendario dello studio")

        self._window.set_study_phase(phase, advice_starts_at)
        in_baseline = phase is StudyPhase.BASELINE
        if in_baseline != self._in_baseline:
            self._in_baseline = in_baseline
            if self._on_baseline_changed is not None:
                self._on_baseline_changed(in_baseline)

        if phase is StudyPhase.ADVICE and not config.study_advice_announced:
            #anche al primo avvio dopo il cambio, se l'app era spenta
            from ecomonitor.notifications import study_advice_started_notification

            config.study_advice_announced = True
            self._save("l'annuncio della fase 2")
            logger.info("Studio: inizia la fase 2, l'app mostra i consigli")
            self._show_window()
            self._show_notice(study_advice_started_notification())

    def _save(self, what: str) -> None:
        try:
            self._config_manager.save()
        except OSError:
            logger.warning("Impossibile salvare %s: resta in memoria", what)


STOP_TIMEOUT_MS = 12_000
#quanto aspettare il thread di polling alla chiusura.

STARTUP_STOP_TIMEOUT_MS = 15_000
#se si esce mentre l'orchestratore e' ancora in costruzione, quella costruzione
#non e' interrompibile

SIGINT_POLL_MS = 500
#ogni quanto restituire il controllo all'interprete perche' possa eseguire un
#handler in sospeso, ed e' anche la latenza massima fra Ctrl+C e la chiusura


def _install_sigint_handler(app: QApplication) -> QTimer:

    def _request_quit(signum=None, *_) -> None:
        if signum is not None:
            signal.signal(signum, signal.SIG_DFL)
        logger.info("Interruzione richiesta: chiusura pulita in corso")
        app.quit()

    for signal_name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, signal_name, None)
        if number is not None:
            signal.signal(number, _request_quit)

    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(SIGINT_POLL_MS)
    return timer


LOG_FILE_NAME = "ecomonitor.log"
LOG_FILE_MAX_BYTES = 2_000_000
LOG_FILE_BACKUPS = 3

LOG_LEVEL_ENV_VAR = "ECOMONITOR_LOG_LEVEL"
DEFAULT_FILE_LOG_LEVEL = logging.INFO

#librerie che a DEBUG riempirebbero il file senza dire niente di utile
NOISY_LOGGERS = ("httpx", "httpcore", "PIL")


def file_log_level(requested: str | None) -> int:
    #Verbosita' del solo file
    if requested is None or not requested.strip():
        return DEFAULT_FILE_LOG_LEVEL

    level = logging.getLevelNamesMapping().get(requested.strip().upper())
    if level is None:
        #un refuso nella variabile non deve far partire l'app senza registro
        logger.warning(
            "%s='%s' non e' un livello valido: uso %s",
            LOG_LEVEL_ENV_VAR,
            requested,
            logging.getLevelName(DEFAULT_FILE_LOG_LEVEL),
        )
        return DEFAULT_FILE_LOG_LEVEL
    return level


def _configure_logging() -> None:
    #Logger di root e' a INFO, cosi' la console resta leggibile anche se la
    #variabile d'ambiente chiede DEBUG. Il file di log ha il livello 
    # richiesto, e contiene anche i logger rumorosi che a DEBUG 
    # inquinerebbero la console
    logging.basicConfig(level=logging.INFO)
    root = logging.getLogger()

    try:
        data_path = platformdirs.user_data_path("ecomonitor")
        data_path.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            data_path / LOG_FILE_NAME,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Impossibile aprire il file di log: resta solo la console")
        return

    level = file_log_level(os.environ.get(LOG_LEVEL_ENV_VAR))
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    #il root fa da cancello
    for existing in root.handlers:
        existing.setLevel(logging.INFO)
    root.addHandler(handler)
    root.setLevel(min(level, logging.INFO))

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    logger.info(
        "Registro su file: %s (livello %s)",
        data_path / LOG_FILE_NAME,
        logging.getLevelName(level),
    )


def _hand_over_if_not_elevated() -> bool:
    if not needs_hand_over(
        platform.system(), getattr(sys, "frozen", False), autostart.is_elevated()
    ):
        return False
    from ecomonitor.gui.single_instance import server_name_for
    from ecomonitor.show_window import request_window

    pipe_name = server_name_for(platformdirs.user_data_path("ecomonitor"))
    return hand_over_to_elevated_instance(
        sys.argv[1:],
        request_window=lambda: request_window(pipe_name),
        launcher_ready=autostart.launcher_task_ready,
        run_launcher=autostart.run_launcher_task,
        relaunch_as_administrator=autostart.relaunch_as_administrator,
    )


def main() -> None:
    _configure_logging()
    #prima di Qt: questo processo serve solo a passare la mano
    if _hand_over_if_not_elevated():
        return

    _force_platform_theme()
    app = QApplication(sys.argv)
    #prima di costruire qualunque widget: cambiare stile dopo li lascerebbe con
    #la tavolozza vecchia
    _apply_dark_aware_style(app)
    #anche sulle finestre di dialogo, che non passano da MainWindow
    app.setWindowIcon(app_icon())
    #un QTimer senza parent raccolto dal garbage collector smette di scattare
    sigint_timer = _install_sigint_handler(app)

    #Non tutti i desktop hanno un'area di notifica
    # Dove non c'e', chiudere chiude
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()

    requested_hidden = hidden_start_requested(sys.argv[1:])

    def pump_and_sleep(seconds: float) -> None:
        app.processEvents()
        QThread.msleep(int(seconds * 1000))

    if waits_for_tray(requested_hidden, platform.system(), tray_available):
        logger.info(
            "Avvio in secondo piano chiesto ma l'area di notifica non c'e' ancora: "
            "aspetto che la shell la crei, fino a %g s", TRAY_WAIT_SECONDS
        )
        tray_available = wait_for_tray(
            QSystemTrayIcon.isSystemTrayAvailable,
            time.monotonic,
            pump_and_sleep,
        )
        if not tray_available:
            logger.warning(
                "L'area di notifica non e' comparsa: EcoMonitor si apre in primo piano"
            )

    app.setQuitOnLastWindowClosed(not tray_available)

    #lo stesso file su cui scrive l'orchestratore: la pagina di storico lo
    #rilegge per conto suo
    window = MainWindow(
        theme_for(_system_prefers_dark(app)), database_path=default_database_path()
    )
    window.hide_on_close = tray_available

    def show_window() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    #il lock va preso prima di costruire l'Orchestrator, che apre il database:
    #due istanze sullo stesso database raddoppiano ogni evento
    guard = SingleInstanceGuard(
        platformdirs.user_data_path("ecomonitor"), on_second_launch=show_window
    )
    if not guard.try_acquire():
        logger.warning(
            "EcoMonitor e' gia' in esecuzione: questa istanza esce senza toccare il database"
        )
        return

    #Dove manca l'area di notifica si puo' comunque restare in secondo piano
    background_available = background_mode_available(
        system=platform.system(),
        tray_available=tray_available,
        can_be_restored=guard.is_listening,
    )
    if background_available:
        window.hide_on_close = True
        app.setQuitOnLastWindowClosed(False)
        #senza tray l'unico Esci è nella finestra
        window.enable_quit_button(app.quit)
        logger.info(
            "Nessuna area di notifica: EcoMonitor continua a misurare in secondo piano. "
            "Rilanciarlo riporta in primo piano la finestra, il pulsante «Esci» lo ferma"
        )

    #SingleInstanceGuard intercetta una seconda GUI
    db_lock = DatabaseLock(default_database_path())
    if not db_lock.acquire():
        guard.release()
        logger.error(
            "Il database e' gia' in uso da un altro processo: "
            " questa istanza esce senza toccarlo"
        )
        QMessageBox.information(None, DATABASE_BUSY_TITLE, database_busy_text(platform.system()))
        return

    #Letta qui, perche' la GUI ne ha bisogno, costa pochi millisecondi
    # La GUI non puo' aspettare il thread di polling perche' deve mostrare subito
    config_manager = ConfigManager()
    config = config_manager.config

    #la fase si applica prima di mostrare qualunque cosa
    initial_phase = config.study_phase(datetime.now(UTC))
    window.set_study_phase(initial_phase, parse_moment(config.study_advice_starts_at))
    if needs_onboarding(getattr(sys, "frozen", False), config):
        window.show_onboarding()

    start_hidden = starts_hidden_for_study(
        requested_hidden,
        initial_phase,
        window.onboarding,
        tray_available,
        background_available,
    )
    if requested_hidden and not start_hidden and not window.onboarding:
        logger.warning(
            "Avvio in secondo piano chiesto ma non possibile su questo desktop: senza "
            "area di notifica e senza modalita' in secondo piano la finestra non si "
            "potrebbe piu' riaprire, quindi EcoMonitor si apre in primo piano"
        )

    if start_hidden:
        macos_dock.set_dock_icon_visible(False)
        logger.info(
            "Avvio in secondo piano: la misurazione parte senza aprire la finestra. "
            "Rilanciare EcoMonitor la riporta in primo piano"
        )
    else:
        window.show()
        window.raise_()
        window.activateWindow()

    started_at = time.monotonic()

    def reopen_if_requested(state: Qt.ApplicationState) -> None:
        #Qt segnala la riapertura dal Dock come stato attivo
        if state != Qt.ApplicationState.ApplicationActive:
            return
        now = time.monotonic()
        if reopens_on_activation(
            platform.system(),
            window.isVisible(),
            start_hidden,
            now - started_at,
            None if window.hidden_at is None else now - window.hidden_at,
        ):
            show_window()

    app.applicationStateChanged.connect(reopen_if_requested)

    default_autostart = DefaultAutostartRunner(config_manager)
    default_autostart.start()

    tray = create_tray_icon(window, on_quit=app.quit) if tray_available else None
    if tray is not None:
        set_study_baseline(tray, initial_phase is StudyPhase.BASELINE)
    if tray is None and not background_available:
        #solo quando la modalita' in secondo piano non e' disponibile
        logger.info(
            "Nessuna area di notifica su questo desktop: l'applicazione non puo' restare "
            "in background, quindi chiudere la finestra la termina. Il monitoraggio "
            "riprende al prossimo avvio"
        )

    def remember_notice_shown() -> None:
        try:
            config_manager.save()
        except OSError:
            logger.warning("Impossibile ricordare che l'avviso di background e' stato mostrato")

    notice_presenter = BackgroundNoticePresenter()

    def announce_background_mode() -> None:
        channel = background_notice_channel(
            tray_available=tray is not None,
            tray_supports_messages=tray is not None and tray.supportsMessages(),
            background_available=background_available,
            tray_notice_shown=config.tray_notice_shown,
            dialog_notice_shown=config.background_notice_shown,
        )
        if channel == "tray" and tray is not None:
            tray.showMessage(
                TRAY_NOTICE_TITLE,
                TRAY_NOTICE_TEXT,
                QSystemTrayIcon.MessageIcon.Information,
                10_000,
            )
            config.tray_notice_shown = True
            remember_notice_shown()
        elif channel == "dialog":
            config.background_notice_shown = True
            remember_notice_shown()
            #fuori da closeEvent: si apre quando la chiusura e' gia' conclusa
            QTimer.singleShot(0, notice_presenter.show)

    window.on_hidden = announce_background_mode

    #creato qui e' quello che rende la consegna
    #accodata invece che diretta
    updater = WindowUpdater(window)

    poll_interval = config.poll_interval_seconds
    #serve alla barra di stato per sapere quando un campionamento e' in ritardo
    window.set_poll_interval(poll_interval)
    #la pagina di storico si rilegge al ritmo con cui il database viene scritto
    window.set_flush_interval(config.db_flush_interval_seconds)

    worker = PollingWorker(config_manager, poll_interval)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.tick_ready.connect(updater.on_tick_ready)
    worker.startup_failed.connect(updater.on_startup_failed)

    window.on_zone_changed = worker.request_zone
    window.on_study_token_changed = worker.request_study_token

    dispatcher = NotificationDispatcher(
        window,
        config,
        lambda: create_notifier(
            tray,
            on_click=show_window,
            data_path=platformdirs.user_data_path("ecomonitor"),
            pipe_name=guard.server_name if guard.is_listening else None,
        ),
    )
    worker.tick_ready.connect(dispatcher.on_tick_ready)
    window.on_notifications_changed = dispatcher.set_switches

    def mark_tray_baseline(in_baseline: bool) -> None:
        if tray is not None:
            set_study_baseline(tray, in_baseline)

    study_controller = StudyPhaseController(
        window,
        config_manager,
        show_window=show_window,
        show_notice=dispatcher.show_notice,
        on_baseline_changed=mark_tray_baseline,
    )
    worker.tick_ready.connect(study_controller.on_tick_ready)
    window.on_study_phase_changed = study_controller.apply_phase
    window.on_quit = app.quit
    window.on_dashboard_visibility_changed = worker.set_dashboard_visible
    #la finestra puo' essere gia' a schermo
    worker.set_dashboard_visible(window.dashboard_on_screen)

    def cleanup() -> None:
        sigint_timer.stop()
        if tray is not None:
            tray.hide()  # senza questo l'icona resta nella tray dopo l'uscita
        worker.stop()
        thread.quit()
        #il salvataggio finale lo fa il worker in fondo a run()
        timeout_ms = STOP_TIMEOUT_MS if worker.has_orchestrator else STARTUP_STOP_TIMEOUT_MS
        if not thread.wait(timeout_ms):
            logger.warning(
                "Il thread di polling non si è fermato entro %.0f s alla chiusura — "
                "probabile tick() o apertura dei sensori bloccati più a lungo del "
                "previsto (driver lento?): l'ultimo intervallo potrebbe non essere "
                "stato salvato",
                timeout_ms / 1000.0,
            )
        #i lock solo dopo il salvataggio finale
        guard.release()
        db_lock.release()

    app.aboutToQuit.connect(cleanup)
    thread.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
