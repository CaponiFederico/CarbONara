import os

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ecomonitor.config import API_KEY_ENV_VAR, ConfigManager

#La configurazione vive in un JSON nella cartella utente

_ATTRIBUTION_MODELS = ("weighted_average", "fifo")

class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, manager: ConfigManager | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("EcoMonitor — impostazioni")
        self._manager = manager or ConfigManager()
        config = self._manager.config

        form = QFormLayout()

        self.zone_edit = QLineEdit(config.em_zone)
        self.zone_edit.setToolTip(
            "Zona di mercato ElectricityMaps, non il codice del paese. "
            "Per l'Italia: IT-NO, IT-CNO (Pisa), IT-CSO, IT-SO, IT-SIC, IT-SAR."
        )
        form.addRow("Zona ElectricityMaps:", self.zone_edit)

        self.country_edit = QLineEdit(config.country_code)
        form.addRow("Codice paese (ISO):", self.country_edit)

        self.api_key_edit = QLineEdit(config.electricitymaps_api_key or "")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("senza chiave si usano le medie statiche")
        form.addRow("Chiave API ElectricityMaps:", self.api_key_edit)

        self.attribution_combo = QComboBox()
        self.attribution_combo.addItems(_ATTRIBUTION_MODELS)
        if config.attribution_model in _ATTRIBUTION_MODELS:
            self.attribution_combo.setCurrentText(config.attribution_model)
        form.addRow("Modello di attribuzione:", self.attribution_combo)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        if os.environ.get(API_KEY_ENV_VAR):
            layout.addWidget(
                QLabel(
                    f"Nota: la variabile d'ambiente {API_KEY_ENV_VAR} è impostata "
                    "e ha la precedenza su questo campo."
                )
            )

        note = QLabel("Le modifiche hanno effetto al prossimo avvio dell'applicazione.")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        config = self._manager.config
        config.em_zone = self.zone_edit.text().strip() or config.em_zone
        config.country_code = (self.country_edit.text().strip() or config.country_code).upper()
        config.electricitymaps_api_key = self.api_key_edit.text().strip() or None
        config.attribution_model = self.attribution_combo.currentText()
        self._manager.save()
        super().accept()
