import logging
import platform
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ecomonitor.config import ConfigManager
from ecomonitor.gui import macos_dock
from ecomonitor.gui.history_page import HistoryPage
from ecomonitor.gui.icon import app_icon
from ecomonitor.gui.settings_dialog import SettingsDialog
from ecomonitor.gui.study_pages import OnboardingPage, StudyPhasePage
from ecomonitor.gui.theme import LIGHT, Theme, build_stylesheet
from ecomonitor.labels import SOURCE_LABELS, grid_source_detail
from ecomonitor.reporting import (
    G_CO2_PER_EMAIL,
    WH_PER_PHONE_CHARGE,
    format_email_count,
    format_energy,
    format_gco2,
    format_phone_charges,
)
from ecomonitor.study_phase import StudyPhase, shows_dashboard

logger = logging.getLogger(__name__)

#le etichette
_SOURCE_LABELS = SOURCE_LABELS

_FIELD_HELP = {
    "power": (
        "Quanta energia stanno usando in questo momento il processore e la scheda "
        "grafica, in watt."
    ),
    "energy": (
        "Quanta energia hanno consumato processore e scheda grafica da mezzanotte "
        "ad adesso. Dipende da quanto tempo il computer è stato acceso e attivo."
    ),
    "operational_gco2": (
        "I grammi di anidride carbonica emessi per produrre l'energia che hai usato. "
        "Dipendono da quanto pulita è la rete elettrica nel momento del consumo, "
        "e da quanto hai consumato."
    ),
    "phone_charges": (
        "Quante volte avresti potuto ricaricare del tutto uno smartphone con l'energia "
        f"che il computer ha consumato oggi, contando {WH_PER_PHONE_CHARGE:.0f} wattora "
        "per ricarica completa."
    ),
    "emails": (
        "A quante email inviate corrispondono i grammi di CO2 emessi oggi dal "
        f"computer, contando {G_CO2_PER_EMAIL:g} grammi per una email breve "
        "scritta e letta su un portatile."
    ),
    "intensity": (
        "Quanto è pulita l'elettricità che stai usando: grammi di CO2 per ogni "
        "kilowattora prodotto."
    ),
    "grid_source": (
        "Da dove arriva il dato sull'elettricità: misurato in tempo reale per la tua "
        "zona, oppure una media annuale del paese quando il dato in tempo reale non "
        "è disponibile."
    ),
    "advisor_charge": (
        "Il computer sta andando a batteria: se conviene collegare l'alimentatore "
        "adesso oppure aspettare un momento in cui l'elettricità sarà più pulita."
    ),
    "advisor_workload": (
        "Il computer sta usando la rete elettrica adesso: se conviene far partire "
        "ora le attività più pesanti, come compilazioni, rendering o giochi, oppure "
        "spostarle a un momento in cui l'elettricità sarà più pulita."
    ),
    "power_state": (
        "Se il computer sta andando a corrente di rete o a batteria, e a che punto "
        "è la carica."
    ),
    "settings": (
        "Zona elettrica, chiave di accesso ai dati in tempo reale e modello di "
        "attribuzione. Paese, zona, chiave e token valgono subito, il resto dal "
        "prossimo avvio."
    ),
    "measurement": (
        "Che cosa l'app sta misurando davvero e che cosa sta stimando. "
        "I sensori della CPU richiedono privilegi di amministratore e, su "
        "Windows, il driver PawnIO: senza, il "
        "consumo del processore viene stimato, e la stima diventa molto più "
        "precisa dopo che l'app ha potuto misurare almeno una volta."
    ),
    "measurement_macos": (
        "Che cosa l'app sta misurando davvero e che cosa sta stimando. "
        "I sensori della CPU richiedono che powermetrics sia autorizzato"
    ),
    "measurement_linux": (
        "Che cosa l'app sta misurando davvero e che cosa sta stimando. "
        "I contatori della CPU (RAPL) vanno resi leggibili una volta con la regola "
        "udev, che i pacchetti .deb e .rpm installano da soli: senza, il consumo del "
        "processore viene stimato, e la stima diventa molto più precisa dopo che "
        "l'app ha potuto misurare almeno una volta."
    ),
    "history": (
        "I totali di oggi, degli ultimi 7 giorni e degli ultimi 30 giorni, con il "
        "consumo medio per fascia oraria."
    ),
}

#Un campionamento in ritardo oltre questo multiplo dell'intervallo di polling
#viene segnalato
_STALE_INTERVALS = 3.0
_STALE_FLOOR_SECONDS = 15.0

_MINIMUM_WIDTH = 460

_STARTING_WIDTH = 700

_LONGEST_ADVICE = (
    "207 gCO2/kWh, il 20% sotto la media prevista per le prossime 12 h: "
    "spostare le attività pesanti alle 05:00 farebbe risparmiare solo il 25%"
)

_ADVICE_SIDE_MARGINS = 64

def measurement_help(system: str) -> str:
    key = {"Darwin": "measurement_macos", "Linux": "measurement_linux"}.get(system, "measurement")
    return _FIELD_HELP[key]

def _format_age(seconds: float) -> str:
    #testo dell'ultimo aggiornamento nella barra di stato
    if seconds < 5:
        return "aggiornato adesso"
    if seconds < 90:
        return f"aggiornato {seconds:.0f} s fa"
    return f"aggiornato {seconds / 60:.0f} min fa"


def _set_value_and_unit(value: QLabel, unit: QLabel | None, formatted: str) -> None:
    #"12.3 Wh" -> numero nella cifra grande, unita' accanto in piccolo
    number, _, suffix = formatted.rpartition(" ")
    value.setText(number)
    if unit is not None:
        unit.setText(suffix)


def _set_state(widget: QWidget, state: str) -> None:
    #Il foglio di stile distingue i casi con la proprieta' 
    # "state" del widget, che puo' essere vuota
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class MainWindow(QMainWindow):
    def __init__(self, theme: Theme | None = None, database_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("EcoMonitor — impatto del tuo utilizzo attivo")
        self.setWindowIcon(app_icon())

        self._fitted_height: int | None = None

        self._theme = theme or LIGHT
        self.setStyleSheet(build_stylesheet(self._theme))

        root = QFrame(self)
        root.setObjectName("root")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(14)

        #il consiglio viene per primo
        layout.addWidget(self._build_advice())
        layout.addLayout(self._build_hero())
        layout.addLayout(self._build_cards())
        layout.addLayout(self._build_details())
        layout.addStretch(1)

        self.dashboard_page = root
        self.history_page = HistoryPage(database_path=database_path, theme=self._theme)
        self.history_page.back_requested.connect(self.show_dashboard)

        #studio: fase 1 e primo avvio prendono il posto di dashboard e storico
        self._study_phase: StudyPhase | None = None
        self._onboarding = False
        self._dashboard_on_screen = False
        self.study_page = StudyPhasePage()
        self.study_page.settings_requested.connect(self.open_settings)
        self.study_page.quit_requested.connect(self._request_quit)
        self.onboarding_page = OnboardingPage()
        self.onboarding_page.joined.connect(self._join_study)
        self.onboarding_page.skipped.connect(self._skip_study)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self.dashboard_page)
        self._stack.addWidget(self.history_page)
        self._stack.addWidget(self.study_page)
        self._stack.addWidget(self.onboarding_page)

        self.setCentralWidget(self._stack)
        self._build_status_bar()
        self.setMinimumWidth(_MINIMUM_WIDTH)
        self._show_page(self.dashboard_page)
        self.resize(max(_STARTING_WIDTH, self.sizeHint().width()), self._starting_height())

        #stato della barra di stato, aggiornata anche fra un tick e l'altro
        self._last_tick_at: float | None = None
        self._poll_interval_seconds = 5.0
        self._active_hours: float | None = None
        #l'orchestratore nasce sul thread di polling
        self._startup_error: str | None = None

        self._status_timer = QTimer(self)  # senza parent verrebbe raccolto e non scatterebbe
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(1000)
        self._refresh_status()

        self.hide_on_close = True
        #Lo decide app.py in base alla presenza di un'area di notifica: senza,
        #nascondersi significherebbe restare vivi e irraggiungibili

        self.on_hidden: Callable[[], None] | None = None
        #quando la finestra e' stata nascosta chiudendola, None se mai
        self.hidden_at: float | None = None
        #lo collega app.py al thread di polling, che possiede il servizio di rete
        self.on_api_key_changed: Callable[[str | None], None] | None = None
        #lo stesso per la zona scelta sulla cartina
        self.on_zone_changed: Callable[[str, str], None] | None = None
        #e per il token dello studio
        self.on_study_token_changed: Callable[[str | None], None] | None = None
        #i due interruttori delle notifiche: consigli, problemi
        self.on_notifications_changed: Callable[[bool, bool], None] | None = None
        #dashboard a schermo o no: il thread di polling la registra per lo studio
        self.on_dashboard_visibility_changed: Callable[[bool], None] | None = None
        #fase dello studio cambiata dalla finestra stessa (benvenuto)
        self.on_study_phase_changed: Callable[[StudyPhase | None], None] | None = None
        #«Esci» della pagina dello studio
        self.on_quit: Callable[[], None] | None = None
        #da dove il benvenuto legge e scrive la configurazione
        self.config_manager_factory: Callable[[], ConfigManager] = ConfigManager
        #chiamata quando la finestra viene nascosta chiudendola: cosa
        #notificare lo decide app.py, la finestra non sa nulla della tray

    # -- costruzione -------------------------------------------------------

    @staticmethod
    def _describe(widget: QWidget, help_text: str) -> None:
        #Il tooltip da solo e' raggiungibile con il solo mouse
        widget.setToolTip(f"<qt>{help_text}</qt>")
        widget.setAccessibleDescription(help_text)

    def _build_hero(self) -> QVBoxLayout:
        #Il dato che si guarda per primo
        self.power_label = QLabel("—")
        self.power_label.setObjectName("heroValue")
        self.power_label.setAccessibleName("Potenza istantanea")

        unit = QLabel("W")
        unit.setObjectName("heroUnit")

        self.power_badge = QLabel("—")
        self.power_badge.setObjectName("powerBadge")
        self.power_badge.setAccessibleName("Alimentazione")
        self._describe(self.power_badge, _FIELD_HELP["power_state"])

        self.power_caption = QLabel("processore + scheda grafica, adesso")
        self.power_caption.setObjectName("heroCaption")

        for widget in (self.power_label, unit, self.power_caption):
            self._describe(widget, _FIELD_HELP["power"])

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.power_label, 0, Qt.AlignmentFlag.AlignBottom)
        top.addWidget(unit, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)
        top.addWidget(self.power_badge, 0, Qt.AlignmentFlag.AlignBottom)

        hero = QVBoxLayout()
        hero.setSpacing(2)
        hero.addLayout(top)
        hero.addWidget(self.power_caption)
        return hero

    def _build_card(
        self, caption: str, unit: str, help_text: str
    ) -> tuple[QFrame, QLabel, QLabel | None]:
        card = QFrame()
        card.setObjectName("metricCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(2)

        label = QLabel(caption)
        label.setObjectName("cardLabel")

        value = QLabel("—")
        value.setObjectName("cardValue")
        value.setAccessibleName(caption)

        value_row = QHBoxLayout()
        value_row.setSpacing(4)
        value_row.addWidget(value, 0, Qt.AlignmentFlag.AlignBottom)

        described = [card, label, value]
        unit_label: QLabel | None = None
        #unit vuoto per le grandezze che non hanno senso in unità fisiche
        if unit:
            unit_label = QLabel(unit)
            unit_label.setObjectName("cardUnit")
            value_row.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignBottom)
            described.append(unit_label)
        value_row.addStretch(1)

        card_layout.addWidget(label)
        card_layout.addLayout(value_row)

        for widget in described:
            self._describe(widget, help_text)
        return card, value, unit_label

    def _build_cards(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(10)

        energy_card, self.energy_label, self.energy_unit_label = self._build_card(
            "energia oggi", "Wh", _FIELD_HELP["energy"]
        )
        charges_card, self.phone_charges_label, _ = self._build_card(
            "ricariche di smartphone", "", _FIELD_HELP["phone_charges"]
        )
        gco2_card, self.operational_gco2_label, self.gco2_unit_label = self._build_card(
            "CO2eq oggi", "g", _FIELD_HELP["operational_gco2"]
        )
        emails_card, self.emails_label, _ = self._build_card(
            "mail inviate", "", _FIELD_HELP["emails"]
        )

        grid.addWidget(energy_card, 0, 0)
        grid.addWidget(charges_card, 0, 1)
        grid.addWidget(gco2_card, 1, 0)
        grid.addWidget(emails_card, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return grid

    def _build_advice(self) -> QFrame:
        self.advice_banner = QFrame()
        self.advice_banner.setObjectName("adviceBanner")
        banner_layout = QVBoxLayout(self.advice_banner)
        banner_layout.setContentsMargins(14, 12, 14, 12)
        banner_layout.setSpacing(4)

        #la didascalia dipende dall'alimentazione 
        self.advice_caption = QLabel("suggerimento")
        self.advice_caption.setObjectName("adviceDetail")

        self.advisor_label = QLabel("—")
        self.advisor_label.setObjectName("adviceTitle")
        self.advisor_label.setWordWrap(True)
        self.advisor_label.setAccessibleName("Suggerimento")

        banner_layout.addWidget(self.advice_caption)
        banner_layout.addWidget(self.advisor_label)

        self._advice_target: str | None = None
        self._describe_advice("charge")

        #la proprieta' va impostata prima che il widget venga disegnato
        _set_state(self.advice_banner, "")
        return self.advice_banner

    def _describe_advice(self, target: str) -> None:
        #target e' il valore di AdviceTarget
        if target == "workload":
            caption = "quando spostare le attività pesanti"
            help_text = _FIELD_HELP["advisor_workload"]
        else:
            caption = "quando ricaricare"
            help_text = _FIELD_HELP["advisor_charge"]
        if self._advice_target is not None:
            self.advice_caption.setText(caption)
            self.advisor_label.setAccessibleName(caption[0].upper() + caption[1:])
        for widget in (self.advice_banner, self.advice_caption, self.advisor_label):
            self._describe(widget, help_text)

    def _build_details(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)

        self.intensity_label = self._add_detail_row(
            grid, 0, "Intensità applicata:", _FIELD_HELP["intensity"]
        )
        self.grid_source_label = self._add_detail_row(
            grid, 1, "Dati rete elettrica:", _FIELD_HELP["grid_source"]
        )
        self.measurement_label = self._add_detail_row(
            grid, 2, "Misura:", measurement_help(platform.system())
        )
        return grid

    def _add_detail_row(
        self, layout: QGridLayout, row: int, caption: str, help_text: str
    ) -> QLabel:
        #la spiegazione va su entrambe le celle
        caption_label = QLabel(caption)
        caption_label.setObjectName("detailKey")

        value_label = QLabel("—")
        value_label.setObjectName("detailValue")
        value_label.setWordWrap(True)
        value_label.setAccessibleName(caption.rstrip(":"))

        for widget in (caption_label, value_label):
            self._describe(widget, help_text)
        layout.addWidget(caption_label, row, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(value_label, row, 1)
        return value_label

    def _build_status_bar(self) -> None:
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(8, 8)
        _set_state(self.status_dot, "")

        self.status_label = QLabel()
        self.status_label.setObjectName("statusText")

        self.history_button = QPushButton("Storico…")
        self.history_button.setObjectName("navButton")
        self.history_button.clicked.connect(self.show_history)
        self._describe(self.history_button, _FIELD_HELP["history"])

        self.settings_button = QPushButton("Impostazioni…")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self.open_settings)
        self._describe(self.settings_button, _FIELD_HELP["settings"])

        #Serve dove chiudere nasconde e non c'e' tray 
        self.quit_button = QPushButton("Esci")
        self.quit_button.setObjectName("navButton")
        self.quit_button.setVisible(False)

        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.addWidget(self.status_dot)
        bar.addWidget(self.status_label, 1)
        bar.addPermanentWidget(self.history_button)
        bar.addPermanentWidget(self.settings_button)
        bar.addPermanentWidget(self.quit_button)

    # -- interazione -------------------------------------------------------

    def _locked_page(self) -> QWidget | None:
        #la pagina che sta al posto di dashboard e storico, se ce n'e' una
        if self._onboarding:
            return self.onboarding_page
        if not shows_dashboard(self._study_phase):
            return self.study_page
        return None

    def _show_page(self, page: QWidget) -> None:
        locked = self._locked_page()
        if locked is not None:
            page = locked
        elif page is self.study_page or page is self.onboarding_page:
            page = self.dashboard_page
        #storico, impostazioni e stato dei campionamenti stanno nella barra
        self.statusBar().setVisible(locked is None)
        for index in range(self._stack.count()):
            candidate = self._stack.widget(index)
            if candidate is None:
                continue  # indice fuori posto: QStackedWidget puo' restituire None
            policy = candidate.sizePolicy()
            wanted_policy = (
                QSizePolicy.Policy.Preferred if candidate is page else QSizePolicy.Policy.Ignored
            )
            policy.setHorizontalPolicy(wanted_policy)
            policy.setVerticalPolicy(wanted_policy)
            candidate.setSizePolicy(policy)

        self._stack.setCurrentWidget(page)
        self._report_dashboard_visibility()

    @property
    def current_page(self) -> QWidget | None:
        return self._stack.currentWidget()

    def show_dashboard(self) -> None:
        self._show_page(self.dashboard_page)

    def show_history(self) -> None:
        self._show_page(self.history_page)
        self.fit_to_content()

    def set_study_phase(
        self, phase: StudyPhase | None, advice_starts_at: datetime | None = None
    ) -> None:
        self.study_page.set_advice_start(advice_starts_at)
        was_locked = self._locked_page()
        self._study_phase = phase
        if self._locked_page() is not was_locked:
            self._show_page(self.dashboard_page)

    @property
    def study_phase(self) -> StudyPhase | None:
        return self._study_phase

    def show_onboarding(self) -> None:
        self._onboarding = True
        self._show_page(self.onboarding_page)

    @property
    def onboarding(self) -> bool:
        return self._onboarding

    def _join_study(self, token: str) -> None:
        try:
            manager = self.config_manager_factory()
            config = manager.config
            config.study_token = token
            config.study_onboarding_done = True
            config.reset_study_calendar()
            manager.save()
        except OSError as exc:
            logger.warning("Codice dello studio non salvato: %s", exc)
            self.onboarding_page.show_error(f"Il codice non è stato salvato: {exc}")
            return
        self._onboarding = False
        if self.on_study_token_changed is not None:
            self.on_study_token_changed(token)
        #fase 1 subito
        self._study_phase = StudyPhase.BASELINE
        self._show_page(self.study_page)
        if self.on_study_phase_changed is not None:
            self.on_study_phase_changed(StudyPhase.BASELINE)

    def _skip_study(self) -> None:
        try:
            manager = self.config_manager_factory()
            manager.config.study_onboarding_done = True
            manager.save()
        except OSError as exc:
            #si chiedera' di nuovo al prossimo avvio
            logger.warning("Scelta di non partecipare non salvata: %s", exc)
        self._onboarding = False
        self._show_page(self.dashboard_page)

    def _request_quit(self) -> None:
        if self.on_quit is not None:
            self.on_quit()

    @property
    def dashboard_on_screen(self) -> bool:
        return self._dashboard_on_screen

    def _report_dashboard_visibility(self) -> None:
        on_screen = (
            self.isVisible()
            and not self.isMinimized()
            and self._stack.currentWidget() is self.dashboard_page
        )
        if on_screen == self._dashboard_on_screen:
            return
        self._dashboard_on_screen = on_screen
        if self.on_dashboard_visibility_changed is not None:
            self.on_dashboard_visibility_changed(on_screen)

    def enable_quit_button(self, on_quit: Callable[[], None]) -> None:
        #chiamato da app.py quando chiudere nasconde ma non c'e' l'area di notifica
        self.quit_button.clicked.connect(on_quit)
        self.quit_button.setVisible(True)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, theme=self._theme)
        dialog.exec()
        #la chiave e la zona si applicano senza riavviare
        if dialog.api_key_changed and self.on_api_key_changed is not None:
            self.on_api_key_changed(dialog.saved_user_api_key)
        if dialog.zone_changed and self.on_zone_changed is not None:
            self.on_zone_changed(dialog.saved_zone, dialog.saved_country)
        if dialog.study_token_changed and self.on_study_token_changed is not None:
            self.on_study_token_changed(dialog.saved_study_token)
        if dialog.notifications_changed and self.on_notifications_changed is not None:
            self.on_notifications_changed(
                dialog.saved_advice_notifications, dialog.saved_problem_notifications
            )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        #l'icona nel Dock torna insieme alla finestra
        macos_dock.set_dock_icon_visible(True)
        self._report_dashboard_visibility()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._report_dashboard_visibility()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._report_dashboard_visibility()
        if self.isMinimized():
            #ridurre la finestra non rimuove l'icona
            return
        if not self.hide_on_close:
            #qui chiudere termina l'applicazione
            return
        self.hidden_at = time.monotonic()
        macos_dock.set_dock_icon_visible(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.hide_on_close:
            #nessuna area di notifica: chiudere deve chiudere davvero
            super().closeEvent(event)
            return

        #chiudere la finestra nasconde l'app nella tray invece di terminarla
        event.ignore()
        self.hide()
        if self.on_hidden is not None:
            self.on_hidden()

    # -- aggiornamenti dal thread di polling -------------------------------

    def set_poll_interval(self, seconds: float) -> None:
        #serve solo a sapere entro quando aspettarsi il campionamento successivo
        self._poll_interval_seconds = seconds

    def set_flush_interval(self, seconds: float) -> None:
        #la pagina di storico legge dal database
        self.history_page.set_refresh_interval(seconds)

    def update_power(self, watts: float, is_estimated: bool) -> None:
        self.power_label.setText(f"{watts:.2f}")
        self.power_caption.setText(
            "processore + scheda grafica, stimati da modello"
            if is_estimated
            else "processore + scheda grafica, adesso"
        )

    def update_power_state(self, is_plugged: bool, battery_percent: float | None) -> None:
        #senza, il consiglio di ricarica non ha contesto
        if is_plugged:
            self.power_badge.setText("da rete elettrica")
            _set_state(self.power_badge, "plugged")
        else:
            charge = f" {battery_percent:.0f}%" if battery_percent is not None else ""
            self.power_badge.setText(f"a batteria{charge}")
            _set_state(self.power_badge, "battery")

    def update_daily_totals(self, energy_wh: float, operational_gco2: float) -> None:
        _set_value_and_unit(self.energy_label, self.energy_unit_label, format_energy(energy_wh))
        _set_value_and_unit(
            self.operational_gco2_label, self.gco2_unit_label, format_gco2(operational_gco2)
        )
        #dall'energia e non dai grammi: l'equivalenza non deve muoversi quando
        #si muove l'intensita' della rete
        self.phone_charges_label.setText(format_phone_charges(energy_wh / WH_PER_PHONE_CHARGE))
        #dai grammi e non dall'energia
        self.emails_label.setText(format_email_count(operational_gco2 / G_CO2_PER_EMAIL))

    def update_active_hours(self, hours: float) -> None:
        self._active_hours = hours

    def update_attribution(
        self,
        intensity_g_per_kwh: float,
        source: str,
        is_estimated: bool,
        grid_source: str | None = None,
        grid_failure: str | None = None,
        zone: str | None = None,
        coverage_ratio: float = 1.0,
    ) -> None:
        label = _SOURCE_LABELS.get(source, source)
        if source == "battery_pool_partial":
            label = f"{label} ({coverage_ratio * 100:.0f}% della carica tracciata)"
        prefix = "~" if is_estimated else ""
        self.intensity_label.setText(f"{prefix}{intensity_g_per_kwh:.0f} gCO2/kWh — {label}")

        if grid_source is not None:
            zone_suffix = f" — zona {zone}" if zone else ""
            grid_label = grid_source_detail(grid_source, grid_failure)
            self.grid_source_label.setText(f"{grid_label}{zone_suffix}")

    def update_measurement_mode(self, mode) -> None:
        if mode.is_complete or not mode.advice:
            self.measurement_label.setText(mode.detail)
        else:
            self.measurement_label.setText(f"{mode.detail} — {mode.advice}")
        _set_state(self.measurement_label, "" if mode.is_complete else "estimated")

    def update_advisor(
        self, text: str, recommendation: str | None = None, target: str | None = None
    ) -> None:
        self.advisor_label.setText(text)
        _set_state(self.advice_banner, recommendation or "")
        if target is not None and target != self._advice_target:
            self._advice_target = target
            self._describe_advice(target)

    def mark_startup_failed(self, reason: str) -> None:
        #il messaggio resta finche' non arriva un tick
        self._startup_error = reason
        self._refresh_status()

    def mark_updated(self) -> None:
        #Serve a sapere entro quando aspettarsi il campionamento successivo, e a
        #aggiornare la barra di stato. Non serve a mostrare il dato, che
        #viene aggiornato direttamente dai metodi di update
        self._last_tick_at = time.monotonic()
        self._refresh_status()

    # -- barra di stato ----------------------------------------------------

    def _refresh_status(self) -> None:
        if self._last_tick_at is None:
            if self._startup_error is not None:
                _set_state(self.status_dot, "stale")
                self.status_label.setText(self._startup_error)
                return
            #copre sia l'apertura dei sensori sia il primo tick
            _set_state(self.status_dot, "")
            self.status_label.setText("in attesa del primo campionamento…")
            return

        age = time.monotonic() - self._last_tick_at
        stale_after = max(_STALE_INTERVALS * self._poll_interval_seconds, _STALE_FLOOR_SECONDS)
        _set_state(self.status_dot, "stale" if age > stale_after else "live")

        parts = [_format_age(age)]
        if self._active_hours is not None:
            parts.append(f"attivo {self._active_hours:.1f} h oggi")
        self.status_label.setText(" · ".join(parts))

    # -- dimensioni --------------------------------------------------------

    def _advice_height_for(self, text: str) -> int:
        #quanto e' alto quel testo nel riquadro del consiglio, alla larghezza
        #di partenza: la misura la fa il font che il foglio di stile ha dato
        #all'etichetta, non un numero di righe fisso
        self.advisor_label.ensurePolished()
        return (
            self.advisor_label.fontMetrics()
            .boundingRect(
                QRect(0, 0, _STARTING_WIDTH - _ADVICE_SIDE_MARGINS, 0),
                int(Qt.TextFlag.TextWordWrap),
                text,
            )
            .height()
        )

    def _starting_height(self) -> int:
        #altezza iniziale della finestra, senza contare i margini
        reserve = self._advice_height_for(_LONGEST_ADVICE) - self._advice_height_for(
            self.advisor_label.text()
        )
        return self.sizeHint().height() + max(0, reserve)

    def _user_shrank_the_window(self) -> bool:
        #Vero se la finestra diversa da quella di default
        return self._fitted_height is not None and self.height() < self._fitted_height

    def _set_height(self, wanted: int) -> None:
        self.resize(self.width(), wanted)
        self._fitted_height = wanted

    def fit_to_content(self) -> None:
        #una volta dopo aver aggiornato le etichette di un tick
        if self._user_shrank_the_window():
            return

        if self._stack.currentWidget() is not self.dashboard_page:
            return

        central = self.centralWidget()
        central_layout = central.layout() if central is not None else None
        if central_layout is not None:
            central_layout.activate()
        needed = self.sizeHint().height()
        if needed > self.height():
            self._set_height(needed)
