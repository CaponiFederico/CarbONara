from collections.abc import Callable

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QGridLayout, QLabel, QMainWindow, QPushButton, QWidget

from ecomonitor.gui.settings_dialog import SettingsDialog

#Le etichette dicono esplicitamente che il numero riguarda l'uso attivo di
#CPU/GPU, mai il "consumo totale del dispositivo": schermo, ventole e disco non
#sono misurati. Intensita' applicata e provenienza del dato di rete sono due
#assi separati: senza chiave API la finestra diceva "in tempo reale" mostrando
#una media annuale.
#La finestra e' passiva: riceve valori gia' pronti dall'orchestratore e non fa
#I/O per conto proprio sul thread della GUI

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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EcoMonitor — impatto del tuo utilizzo attivo")

        central = QWidget(self)
        layout = QGridLayout(central)

        layout.addWidget(QLabel("Potenza istantanea (CPU+GPU):"), 0, 0)
        self.power_label = QLabel("—")
        layout.addWidget(self.power_label, 0, 1)

        layout.addWidget(QLabel("Energia uso attivo oggi:"), 1, 0)
        self.energy_label = QLabel("—")
        layout.addWidget(self.energy_label, 1, 1)

        layout.addWidget(QLabel("CO2eq operativa oggi:"), 2, 0)
        self.operational_gco2_label = QLabel("—")
        layout.addWidget(self.operational_gco2_label, 2, 1)

        layout.addWidget(QLabel("Intensità applicata:"), 3, 0)
        self.intensity_label = QLabel("—")
        layout.addWidget(self.intensity_label, 3, 1)

        layout.addWidget(QLabel("Dati rete elettrica:"), 4, 0)
        self.grid_source_label = QLabel("—")
        layout.addWidget(self.grid_source_label, 4, 1)

        layout.addWidget(QLabel("Suggerimento ricarica:"), 5, 0)
        self.advisor_label = QLabel("—")
        layout.addWidget(self.advisor_label, 5, 1)

        self.settings_button = QPushButton("Impostazioni…")
        self.settings_button.clicked.connect(self.open_settings)
        layout.addWidget(self.settings_button, 6, 0, 1, 2)

        self.setCentralWidget(central)

        self.on_hidden: Callable[[], None] | None = None
        #chiamata quando la finestra viene nascosta chiudendola: cosa
        #notificare lo decide app.py, la finestra non sa nulla della tray

    # -- interazione -------------------------------------------------------

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        #chiudere la finestra nasconde l'app nella tray invece di terminarla:
        #il monitoraggio e' continuo e un processo terminato non vede le
        #ricariche. Chi chiude crede pero' di aver chiuso l'applicazione,
        #quindi la prima volta va detto (vedi il callback)
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
