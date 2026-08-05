import logging
import signal
import sys

import platformdirs
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ecomonitor.advisor import ChargeAdvice
from ecomonitor.gui.main_window import MainWindow
from ecomonitor.gui.single_instance import SingleInstanceGuard
from ecomonitor.gui.tray import create_tray_icon
from ecomonitor.orchestrator import Orchestrator

#Qui resta solo cio' che riguarda la GUI: la logica di misura e attribuzione
#sta in ecomonitor.orchestrator

#Orchestrator.tick() fa I/O bloccante verso i driver (decine o centinaia di ms):
#sul thread della GUI l'event loop di Qt si blocca e la finestra smette di
#rispondere. PollingWorker lo sposta su un QThread dedicato e comunica solo via
#segnali

logger = logging.getLogger(__name__)


class PollingWorker(QObject):
    #esegue tick() fuori dal thread della GUI: mai toccare i widget da qui

    tick_ready = Signal(dict)

    def __init__(self, orchestrator: Orchestrator, poll_interval_seconds: float) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._poll_interval_ms = int(poll_interval_seconds * 1000)
        self._running = True

    @Slot()
    def run(self) -> None:
        while self._running:
            try:
                result = self._orchestrator.tick()
                self.tick_ready.emit(result)
            except Exception:
                #un tick fallito (sensore momentaneamente non leggibile, disco
                #occupato) non deve bloccare l'app: si riprova al prossimo intervallo
                logger.exception("Tick fallito, si riprova al prossimo intervallo")

            self._interruptible_sleep(self._poll_interval_ms)

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


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    app = QApplication(sys.argv)
    #un QTimer senza parent raccolto dal garbage collector smette di scattare
    sigint_timer = _install_sigint_handler(app)
    #l'app e' un monitor continuo: chiudere la finestra la nasconde nella tray
    #invece di terminare il processo, altrimenti ogni chiusura farebbe perdere
    #di vista le ricariche
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()

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

    orchestrator = Orchestrator()
    tray = create_tray_icon(window, on_quit=app.quit)

    def announce_background_mode() -> None:
        #una volta sola nella vita dell'installazione
        config = orchestrator.config_manager.config
        if config.tray_notice_shown or not tray.supportsMessages():
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
            orchestrator.config_manager.save()
        except OSError:
            logger.warning("Impossibile ricordare che l'avviso di background e' stato mostrato")

    window.on_hidden = announce_background_mode
    window.show()

    def on_tick_ready(result: dict) -> None:
        reading = result["reading"]
        attribution = result["attribution"]
        window.update_power(reading.total_w, reading.is_estimated)
        #i totali arrivano gia' calcolati dal thread di polling: la finestra
        #non deve interrogare SQLite per conto suo
        window.update_daily_totals(result["today_energy_wh"], result["today_operational_gco2"])
        window.update_attribution(
            attribution.intensity_g_per_kwh,
            attribution.source.value,
            attribution.is_estimated_intensity,
            grid_source=result["grid_intensity_source"],
            zone=result["grid_zone"],
            coverage_ratio=attribution.pool_coverage_ratio,
        )

        advice: ChargeAdvice = result["advice"]
        window.update_advisor(advice.message())

    worker = PollingWorker(orchestrator, orchestrator.config_manager.config.poll_interval_seconds)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.tick_ready.connect(on_tick_ready)

    def cleanup() -> None:
        sigint_timer.stop()
        guard.release()
        tray.hide()  # senza questo l'icona resta nella tray dopo l'uscita
        worker.stop()
        thread.quit()
        if not thread.wait(3000):
            logger.warning(
                "Il thread di polling non si è fermato entro 3s alla chiusura — "
                "probabile tick() bloccato più a lungo del previsto (driver lento?)"
            )
        orchestrator.flush()

    app.aboutToQuit.connect(cleanup)
    thread.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
