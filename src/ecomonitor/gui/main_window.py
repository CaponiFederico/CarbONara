import time
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ecomonitor.gui.icon import app_icon
from ecomonitor.gui.settings_dialog import SettingsDialog
from ecomonitor.gui.theme import LIGHT, Theme, build_stylesheet
from ecomonitor.reporting import WH_PER_PHONE_CHARGE, format_phone_charges

#definisce le etichette per le fonti di dati, perche' alcune sono
#tecniche e poco leggibili. La label viene mostrata nella finestra principale

_SOURCE_LABELS = {
    "grid_live": "consumo da rete elettrica",
    "battery_pool": "storico di ricarica della batteria",
    "battery_pool_partial": "storico di ricarica parziale",
    "battery_pool_fallback": "stima (nessuno storico di ricarica disponibile)",
}

_GRID_SOURCE_LABELS = {
    "live": "dati in tempo reale",
    "static_fallback": "media statica di ripiego, nessuna chiave API",
}


#Spiegazioni per chi non e' tecnico: compaiono passando il mouse su una riga
#Descrivono il significato del dato e non del valore

_FIELD_HELP = {
    "power": (
        "Quanta energia stanno usando in questo momento il processore e la scheda "
        "grafica, in watt. Non è il consumo totale del computer: schermo, ventole "
        "e disco non sono inclusi."
    ),
    "energy": (
        "Quanta energia hanno consumato processore e scheda grafica da mezzanotte "
        "a adesso. Un wattora (Wh) è l'energia che consuma un apparecchio da 1 watt "
        "acceso per un'ora."
    ),
    "operational_gco2": (
        "I grammi di anidride carbonica emessi per produrre l'energia che hai usato "
        "oggi. Dipendono da quanta ne hai consumata e da quanto era pulita la rete "
        "elettrica nel momento in cui l'hai consumata."
    ),
    "phone_charges": (
        "Quante volte avresti potuto ricaricare del tutto uno smartphone con l'energia "
        f"che il computer ha consumato oggi, contando {WH_PER_PHONE_CHARGE:.0f} wattora "
        "per ricarica completa. È un confronto fra energie: non dipende da quanto era "
        "pulita la rete elettrica."
    ),
    "intensity": (
        "Quanto è pulita l'elettricità che stai usando: grammi di CO2 per ogni "
        "kilowattora prodotto. In Italia va da circa 100 quando c'è molto sole e "
        "vento, a oltre 400 quando la rete va soprattutto a gas. Più basso è meglio."
    ),
    "grid_source": (
        "Da dove arriva il dato sull'elettricità: misurato in tempo reale per la tua "
        "zona, oppure una media annuale del paese quando il dato in tempo reale non "
        "è disponibile."
    ),
    "advisor": (
        "Se conviene collegare l'alimentatore adesso oppure aspettare un momento in "
        "cui l'elettricità sarà più pulita. Non compare sui computer fissi, che non "
        "hanno una batteria da ricaricare."
    ),
    "power_state": (
        "Se il computer sta andando a corrente di rete o a batteria, e a che punto "
        "è la carica. Il consiglio di ricarica ha senso solo a batteria."
    ),
    "settings": (
        "Zona elettrica, chiave di accesso ai dati in tempo reale e modello di "
        "attribuzione. Le modifiche valgono dal prossimo avvio."
    ),
}

#Un campionamento in ritardo oltre questo multiplo dell'intervallo di polling
#viene segnalato
_STALE_INTERVALS = 3.0
_STALE_FLOOR_SECONDS = 15.0

_MINIMUM_WIDTH = 460


def _format_age(seconds: float) -> str:
    #testo dell'ultimo aggiornamento nella barra di stato
    if seconds < 5:
        return "aggiornato adesso"
    if seconds < 90:
        return f"aggiornato {seconds:.0f} s fa"
    return f"aggiornato {seconds / 60:.0f} min fa"


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
    def __init__(self, theme: Theme | None = None) -> None:
        super().__init__()
        self.setWindowTitle("EcoMonitor — impatto del tuo utilizzo attivo")
        self.setWindowIcon(app_icon())

        self._theme = theme or LIGHT
        self.setStyleSheet(build_stylesheet(self._theme))

        root = QFrame(self)
        root.setObjectName("root")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(14)

        layout.addLayout(self._build_hero())
        layout.addLayout(self._build_cards())
        layout.addWidget(self._build_advice())
        layout.addLayout(self._build_details())
        layout.addStretch(1)

        self.setCentralWidget(root)
        self._build_status_bar()
        self.setMinimumWidth(_MINIMUM_WIDTH)

        #stato della barra di stato, aggiornata anche fra un tick e l'altro
        self._last_tick_at: float | None = None
        self._poll_interval_seconds = 5.0
        self._active_hours: float | None = None

        self._status_timer = QTimer(self)  # senza parent verrebbe raccolto e non scatterebbe
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(1000)
        self._refresh_status()

        self.hide_on_close = True
        #Lo decide app.py in base alla presenza di un'area di notifica: senza,
        #nascondersi significherebbe restare vivi e irraggiungibili

        self.on_hidden: Callable[[], None] | None = None
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

    def _build_card(self, caption: str, unit: str, help_text: str) -> tuple[QFrame, QLabel]:
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
        return card, value

    def _build_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        energy_card, self.energy_label = self._build_card(
            "energia oggi", "Wh", _FIELD_HELP["energy"]
        )
        gco2_card, self.operational_gco2_label = self._build_card(
            "CO2eq oggi", "g", _FIELD_HELP["operational_gco2"]
        )
        charges_card, self.phone_charges_label = self._build_card(
            "ricariche di smartphone", "", _FIELD_HELP["phone_charges"]
        )

        for card in (energy_card, gco2_card, charges_card):
            row.addWidget(card, 1)
        return row

    def _build_advice(self) -> QFrame:
        self.advice_banner = QFrame()
        self.advice_banner.setObjectName("adviceBanner")
        banner_layout = QVBoxLayout(self.advice_banner)
        banner_layout.setContentsMargins(12, 10, 12, 10)
        banner_layout.setSpacing(2)

        caption = QLabel("suggerimento ricarica")
        caption.setObjectName("adviceDetail")

        self.advisor_label = QLabel("—")
        self.advisor_label.setObjectName("adviceTitle")
        self.advisor_label.setWordWrap(True)
        self.advisor_label.setAccessibleName("Suggerimento ricarica")

        banner_layout.addWidget(caption)
        banner_layout.addWidget(self.advisor_label)

        for widget in (self.advice_banner, caption, self.advisor_label):
            self._describe(widget, _FIELD_HELP["advisor"])

        #la proprieta' va impostata prima che il widget venga disegnato
        _set_state(self.advice_banner, "")
        return self.advice_banner

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

        self.settings_button = QPushButton("Impostazioni…")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self.open_settings)
        self._describe(self.settings_button, _FIELD_HELP["settings"])

        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.addWidget(self.status_dot)
        bar.addWidget(self.status_label, 1)
        bar.addPermanentWidget(self.settings_button)

    # -- interazione -------------------------------------------------------

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

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
        self.energy_label.setText(f"{energy_wh:.1f}")
        self.operational_gco2_label.setText(f"{operational_gco2:.1f}")
        #dall'energia e non dai grammi: l'equivalenza non deve muoversi quando
        #si muove l'intensita' della rete
        self.phone_charges_label.setText(format_phone_charges(energy_wh / WH_PER_PHONE_CHARGE))

    def update_active_hours(self, hours: float) -> None:
        self._active_hours = hours

    def update_attribution(
        self,
        intensity_g_per_kwh: float,
        source: str,
        is_estimated: bool,
        grid_source: str | None = None,
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
            grid_label = _GRID_SOURCE_LABELS.get(grid_source, grid_source)
            self.grid_source_label.setText(f"{grid_label}{zone_suffix}")

    def update_advisor(self, text: str, recommendation: str | None = None) -> None:
        #recommendation e' il valore di ChargeRecommendation
        self.advisor_label.setText(text)
        _set_state(self.advice_banner, recommendation or "")

    def mark_updated(self) -> None:
        #Serve a sapere entro quando aspettarsi il campionamento successivo, e a
        #aggiornare la barra di stato. Non serve a mostrare il dato, che
        #viene aggiornato direttamente dai metodi di update
        self._last_tick_at = time.monotonic()
        self._refresh_status()

    # -- barra di stato ----------------------------------------------------

    def _refresh_status(self) -> None:
        if self._last_tick_at is None:
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

    def fit_to_content(self) -> None:
        #va chiamato una volta dopo aver aggiornato le etichette di un tick
        central = self.centralWidget()
        central_layout = central.layout() if central is not None else None
        if central_layout is not None:
            central_layout.activate()
        needed = self.sizeHint().height()
        if needed > self.height():
            self.resize(self.width(), needed)
