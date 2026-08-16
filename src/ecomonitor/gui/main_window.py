from collections.abc import Callable

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QGridLayout, QLabel, QMainWindow, QPushButton, QWidget

from ecomonitor.gui.settings_dialog import SettingsDialog

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
    "settings": (
        "Zona elettrica, chiave di accesso ai dati in tempo reale e modello di "
        "attribuzione. Le modifiche valgono dal prossimo avvio."
    ),
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EcoMonitor — impatto del tuo utilizzo attivo")

        central = QWidget(self)
        layout = QGridLayout(central)
        #la colonna dei valori deve poter crescere
        layout.setColumnStretch(1, 1)

        self.power_label = self._add_row(
            layout, 0, "Potenza istantanea (CPU+GPU):", _FIELD_HELP["power"]
        )
        self.energy_label = self._add_row(
            layout, 1, "Energia uso attivo oggi:", _FIELD_HELP["energy"]
        )
        self.operational_gco2_label = self._add_row(
            layout, 2, "CO2eq operativa oggi:", _FIELD_HELP["operational_gco2"]
        )
        self.intensity_label = self._add_row(
            layout, 3, "Intensità applicata:", _FIELD_HELP["intensity"]
        )
        self.grid_source_label = self._add_row(
            layout, 4, "Dati rete elettrica:", _FIELD_HELP["grid_source"]
        )
        self.advisor_label = self._add_row(
            layout, 5, "Suggerimento ricarica:", _FIELD_HELP["advisor"]
        )

        self.settings_button = QPushButton("Impostazioni…")
        self.settings_button.clicked.connect(self.open_settings)
        self._describe(self.settings_button, _FIELD_HELP["settings"])
        layout.addWidget(self.settings_button, 6, 0, 1, 2)

        self.setCentralWidget(central)

        #calibrata prima del primo show()
        self._calibrate_size()

        self.hide_on_close = True
        #Lo decide app.py in base alla presenza di un'area di notifica: senza,
        #nascondersi significherebbe restare vivi e irraggiungibili

        self.on_hidden: Callable[[], None] | None = None
        #chiamata quando la finestra viene nascosta chiudendola: cosa
        #notificare lo decide app.py, la finestra non sa nulla della tray

    # -- costruzione -------------------------------------------------------

    def _calibrate_size(self) -> None:
        #le stringhe piu' lunghe 
        self.intensity_label.setText(
            "~320 gCO2/kWh — storico di ricarica parziale (73% della carica tracciata)"
        )
        self.advisor_label.setText(
            "conviene aspettare le 09:00: 206 invece di 357 gCO2/kWh (-42%)"
        )
        self.grid_source_label.setText(
            "media statica di ripiego, nessuna chiave API — zona IT-CNO"
        )
        self._activate_layout()
        self.resize(self.sizeHint())
        self.setMinimumSize(self.size())

        for label in (self.intensity_label, self.advisor_label, self.grid_source_label):
            label.setText("—")

    @staticmethod
    def _describe(widget: QWidget, help_text: str) -> None:
        #Il tooltip da solo e' raggiungibile con il solo mouse
        widget.setToolTip(f"<qt>{help_text}</qt>")
        widget.setAccessibleDescription(help_text)

    def _add_row(self, layout: QGridLayout, row: int, caption: str, help_text: str) -> QLabel:
        #la spiegazione va su entrambe le cellei
        caption_label = QLabel(caption)
        value_label = QLabel("—")
        value_label.setWordWrap(True)
        value_label.setAccessibleName(caption.rstrip(":"))
        for widget in (caption_label, value_label):
            self._describe(widget, help_text)
        layout.addWidget(caption_label, row, 0)
        layout.addWidget(value_label, row, 1)
        return value_label

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

    def update_power(self, watts: float, is_estimated: bool) -> None:
        suffix = " (stima)" if is_estimated else ""
        self.power_label.setText(f"{watts:.2f} W{suffix}")

    def update_daily_totals(self, energy_wh: float, operational_gco2: float) -> None:
        self.energy_label.setText(f"{energy_wh:.1f} Wh")
        self.operational_gco2_label.setText(f"{operational_gco2:.1f} g CO2eq")

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
            self.grid_source_label.setText(
                f"{_GRID_SOURCE_LABELS.get(grid_source, grid_source)}{zone_suffix}"
            )

    def update_advisor(self, text: str) -> None:
        self.advisor_label.setText(text)

    def _activate_layout(self) -> None:
        central = self.centralWidget()
        layout = central.layout() if central is not None else None
        if layout is not None:
            layout.activate()

    def fit_to_content(self) -> None:
        #va chiamato una volta dopo aver aggiornato le etichette di un tick
        self._activate_layout()
        hint = self.sizeHint()
        new_width = max(self.width(), hint.width())
        new_height = max(self.height(), hint.height())
        if (new_width, new_height) != (self.width(), self.height()):
            self.resize(new_width, new_height)
