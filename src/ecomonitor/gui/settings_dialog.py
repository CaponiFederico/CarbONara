import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ecomonitor import autostart
from ecomonitor.config import (
    API_KEY_ENV_VAR,
    ATTRIBUTION_MODELS,
    ConfigManager,
    packaged_api_key,
)

#La configurazione vive in un JSON nella cartella utente

#la lista viene da config.ATTRIBUTION_MODELS
_ATTRIBUTION_MODELS = ATTRIBUTION_MODELS

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
        self.api_key_edit.setPlaceholderText(
            "una chiave e' gia' inclusa: qui se puoi metterene una propria"
            if packaged_api_key()
            else "senza chiave si usano le medie statiche"
        )
        form.addRow("Chiave API ElectricityMaps:", self.api_key_edit)

        self.attribution_combo = QComboBox()
        self.attribution_combo.addItems(_ATTRIBUTION_MODELS)
        if config.attribution_model in _ATTRIBUTION_MODELS:
            self.attribution_combo.setCurrentText(config.attribution_model)
        form.addRow("Modello di attribuzione:", self.attribution_combo)

        self.autostart_check = QCheckBox("Avvia EcoMonitor all'accesso")
        self._autostart_supported = autostart.is_supported()
        if self._autostart_supported:
            self.autostart_check.setChecked(autostart.is_enabled())
            self.autostart_check.setToolTip(
                "Avvio con i sensori hardware leggibili"
            )
        else:
            self.autostart_check.setChecked(False)
            self.autostart_check.setEnabled(False)
            self.autostart_check.setToolTip(autostart.unsupported_reason())
        form.addRow("Avvio automatico:", self.autostart_check)

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

    def _apply_autostart(self) -> bool:
        #Torna False se la finestra deve restare aperta
        if not self._autostart_supported:
            return True
        wanted = self.autostart_check.isChecked()
        if wanted == autostart.is_enabled():
            return True
        try:
            autostart.set_enabled(wanted)
        except autostart.AutostartError as exc:
            QMessageBox.warning(self, "Avvio automatico non modificato", str(exc))
            #la casella torna a dire la verita'
            self.autostart_check.setChecked(autostart.is_enabled())
            return False
        return True

    def accept(self) -> None:
        if not self._apply_autostart():
            return
        config = self._manager.config
        config.em_zone = self.zone_edit.text().strip() or config.em_zone
        config.country_code = (self.country_edit.text().strip() or config.country_code).upper()
        config.electricitymaps_api_key = self.api_key_edit.text().strip() or None
        config.attribution_model = self.attribution_combo.currentText()
        try:
            self._manager.save()
        except OSError as exc:
            
            QMessageBox.warning(
                self,
                "Impossibile salvare le impostazioni",
                f"Le modifiche non sono state salvate: {exc}",
            )
            return
        super().accept()
