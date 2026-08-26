from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

import platformdirs
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QSystemTrayIcon

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

    @property
    def has_orchestrator(self) -> bool:
        #letto dal thread della GUI alla chiusura
        return self._orchestrator is not None

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

        while self._running:
            try:
                result = self._orchestrator.tick()
                self.tick_ready.emit(result)
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
        while elapsed < total_ms and self._running:
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
            zone=result["grid_zone"],
            coverage_ratio=attribution.pool_coverage_ratio,
        )

        advice: ChargeAdvice = result["advice"]
        #la raccomandazione viaggia accanto al testo: il messaggio da solo non
        #dice alla finestra di che colore dipingere il riquadro
        self._window.update_advisor(advice.message(), advice.recommendation.value)

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
    #Qt non gestisce SIGINT: senza questo Ctrl+C non chiude l'app, e su Windows
    #non arriva neanche KeyboardInterrupt. Il timer scatta ogni mezzo secondo
    #per restituire il controllo all'interprete e far eseguire l'handler

    def _request_quit(*_) -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)  # un secondo Ctrl+C forza
        logger.info("Interruzione richiesta: chiusura pulita in corso")
        app.quit()

    signal.signal(signal.SIGINT, _request_quit)

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

    #SingleInstanceGuard intercetta una seconda GUI 
    db_lock = DatabaseLock(default_database_path())
    if not db_lock.acquire():
        guard.release()
        logger.error(
            "Il database e' gia' in uso da un altro processo (probabilmente "
            "ecomonitor-cli): questa istanza esce senza toccarlo"
        )
        return

    #Letta qui, perche' la GUI ne ha bisogno, costa pochi millisecondi
    # La GUI non puo' aspettare il thread di polling perche' deve mostrare subito
    config_manager = ConfigManager()
    config = config_manager.config

    #mostrata subito, e da qui in poi il thread della GUI non fa piu' niente di
    #lungo: la finestra risponde mentre i sensori si aprono altrove
    window.show()
    window.raise_()
    window.activateWindow()

    tray = create_tray_icon(window, on_quit=app.quit) if tray_available else None
    if tray is None:
        logger.info(
            "Nessuna area di notifica su questo desktop: l'applicazione non puo' restare "
            "in background, quindi chiudere la finestra la termina. Il monitoraggio "
            "riprende al prossimo avvio"
        )

    def announce_background_mode() -> None:
        #una volta sola nella vita dell'installazione
        if tray is None or config.tray_notice_shown or not tray.supportsMessages():
            return
        tray.showMessage(
            "EcoMonitor continua a misurare",
            "La finestra è nascosta, l'app resta attiva nella barra delle "
            "applicazioni. Per uscire davvero: clic destro sull'icona → Esci.",
            QSystemTrayIcon.MessageIcon.Information,
            10_000,
        )
        config.tray_notice_shown = True
        try:
            config_manager.save()
        except OSError:
            logger.warning("Impossibile ricordare che l'avviso di background e' stato mostrato")

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
