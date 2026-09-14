from __future__ import annotations

import logging
import os
import platform
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import platformdirs
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory, QSystemTrayIcon

from ecomonitor import autostart
from ecomonitor.config import ConfigManager
from ecomonitor.gui.icon import app_icon
from ecomonitor.gui.main_window import MainWindow
from ecomonitor.gui.single_instance import SingleInstanceGuard
from ecomonitor.gui.theme import theme_for
from ecomonitor.gui.tray import create_tray_icon
from ecomonitor.storage.db import default_database_path
from ecomonitor.storage.lock import DatabaseLock

if TYPE_CHECKING:
    #a runtime li importa il thread di polling
    from ecomonitor.advisor import ChargeAdvice
    from ecomonitor.orchestrator import Orchestrator

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
    supported: bool, already_applied: bool, already_enabled: bool, needs_elevation: bool
) -> DefaultAutostartAction:
    #L'avvio automatico nasce attivo, ma l'utente puo' disattivarlo
    if not supported or already_applied:
        return "skip"
    if already_enabled:
        #gia' attivo
        return "record"
    if needs_elevation:
        return "wait"
    return "enable"


def apply_default_autostart(config_manager: ConfigManager) -> None:
    config = config_manager.config
    supported = autostart.is_supported()
    already_applied = config.autostart_default_applied
    decided = not supported or already_applied
    action = default_autostart_action(
        supported=supported,
        already_applied=already_applied,
        already_enabled=False if decided else autostart.is_enabled(),
        needs_elevation=False if decided else autostart.needs_elevation(),
    )
    if action in ("skip", "wait"):
        return

    if action == "enable":
        try:
            autostart.set_enabled(True)
        except autostart.AutostartError as exc:
            logger.warning("Non riesco ad attivare l'avvio automatico: %s", exc)
            return
        logger.info(
            "Avvio automatico attivato: EcoMonitor si aprira' all'accesso, in primo "
            "piano. Si disattiva dalle impostazioni"
        )

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

        #Richiesta di cambio chiave o zona
        self._pending_lock = threading.Lock()
        self._pending_user_api_key: tuple[str | None, ...] = ()
        self._pending_zone: tuple[str, str | None] | None = None

    def _backfill_grid_history(self) -> None:
        #Un errore non deve fermare il monitoraggio
        if self._orchestrator is None:
            return
        try:
            self._orchestrator.backfill_grid_history()
        except Exception:
            logger.exception("Riempimento dello storico di rete non riuscito")

    @property
    def has_orchestrator(self) -> bool:
        #letto dal thread della GUI alla chiusura
        return self._orchestrator is not None

    def request_user_api_key(self, user_api_key: str | None) -> None:
        #chiamata dal thread della GUI quando le impostazioni vengono salvate
        with self._pending_lock:
            self._pending_user_api_key = (user_api_key,)

    def _take_pending_user_api_key(self) -> tuple[str | None, ...]:
        with self._pending_lock:
            pending, self._pending_user_api_key = self._pending_user_api_key, ()
        return pending

    def _apply_pending_user_api_key(self) -> None:
        pending = self._take_pending_user_api_key()
        if not pending or self._orchestrator is None:
            return
        try:
            self._orchestrator.apply_user_api_key(pending[0])
        except Exception:
            #una chiave rifiutata non deve fermare la misura
            logger.exception("Chiave ElectricityMaps non applicata")

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

    def _has_pending_request(self) -> bool:
        with self._pending_lock:
            return bool(self._pending_user_api_key) or self._pending_zone is not None

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
            #prima del tick: cosi' il primo tick dopo il salvataggio interroga
            #gia' l'API invece di restituire ancora il ripiego
            self._apply_pending_user_api_key()
            zone_changed = self._apply_pending_zone()
            try:
                result = self._orchestrator.tick()
                self.tick_ready.emit(result)
                if not first_tick_done:
                    first_tick_done = True
                    self._backfill_grid_history()
                elif zone_changed:
                    #dopo il tick 
                    self._backfill_grid_history()
            except Exception:
                #un tick fallito (sensore momentaneamente non leggibile, disco
                #occupato) non deve bloccare l'app: si riprova al prossimo intervallo
                logger.exception("Tick fallito, si riprova al prossimo intervallo")

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
            advice.message(), advice.recommendation.value, advice.target.value
        )

        self._window.update_measurement_mode(result["measurement_mode"])
        self._window.fit_to_content()
        self._window.mark_updated()

    @Slot(str)
    def on_startup_failed(self, reason: str) -> None:
        self._window.mark_startup_failed(reason)


STOP_TIMEOUT_MS = 3000
#quanto aspettare il thread di polling alla chiusura

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


def main() -> None:
    _configure_logging()

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
        return

    #Letta qui, perche' la GUI ne ha bisogno, costa pochi millisecondi
    # La GUI non puo' aspettare il thread di polling perche' deve mostrare subito
    config_manager = ConfigManager()
    config = config_manager.config

    start_hidden = should_start_hidden(requested_hidden, tray_available, background_available)
    if requested_hidden and not start_hidden:
        logger.warning(
            "Avvio in secondo piano chiesto ma non possibile su questo desktop: senza "
            "area di notifica e senza modalita' in secondo piano la finestra non si "
            "potrebbe piu' riaprire, quindi EcoMonitor si apre in primo piano"
        )

    if start_hidden:
        logger.info(
            "Avvio in secondo piano: la misurazione parte senza aprire la finestra. "
            "Rilanciare EcoMonitor la riporta in primo piano"
        )
    else:
        window.show()
        window.raise_()
        window.activateWindow()

 
    QTimer.singleShot(0, lambda: apply_default_autostart(config_manager))

    tray = create_tray_icon(window, on_quit=app.quit) if tray_available else None
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

    window.on_api_key_changed = worker.request_user_api_key
    window.on_zone_changed = worker.request_zone

    def cleanup() -> None:
        sigint_timer.stop()
        guard.release()
        db_lock.release()
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

    app.aboutToQuit.connect(cleanup)
    thread.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
