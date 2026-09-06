import os

from PySide6.QtGui import QIcon
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
    AppConfig,
    ConfigManager,
    packaged_api_key,
)

#La configurazione vive in un JSON nella cartella utente

#la lista viene da config.ATTRIBUTION_MODELS
_ATTRIBUTION_MODELS = ATTRIBUTION_MODELS


def user_api_key(config: AppConfig) -> str | None:
    #La chiave dell'utente l'unica che il campo puo' mostrare
    saved = config.electricitymaps_api_key
    if saved and saved == packaged_api_key():
        return None
    return saved

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

        self._saved_key = user_api_key(config)
        self.saved_user_api_key = self._saved_key
        self.api_key_changed = False
        self.api_key_edit = QLineEdit(self._saved_key or "")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(self._api_key_placeholder())
        self.api_key_edit.setToolTip(
            "La tua chiave ha la precedenza su quella inclusa. "
            "Per tornare a quella inclusa: cancella il campo e salva."
            if packaged_api_key()
            else "Per toglierla: cancella il campo e salva."
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

        self.hidden_start_check = QCheckBox("Parti in secondo piano, senza aprire la finestra")
        if self._autostart_supported:
            self.hidden_start_check.setChecked(autostart.starts_hidden())
            #senza avvio automatico non c'e' niente da far partire nascosto
            self.hidden_start_check.setEnabled(self.autostart_check.isChecked())
            self.autostart_check.toggled.connect(self.hidden_start_check.setEnabled)
            self.hidden_start_check.setToolTip(
                "Vale solo all'accesso, se EcoMonitor parte automaticamente"
            )
        else:
            self.hidden_start_check.setChecked(False)
            self.hidden_start_check.setEnabled(False)
            self.hidden_start_check.setToolTip(autostart.unsupported_reason())
        form.addRow("", self.hidden_start_check)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        if os.environ.get(API_KEY_ENV_VAR):
            layout.addWidget(
                QLabel(
                    f"Nota: la variabile d'ambiente {API_KEY_ENV_VAR} è impostata "
                    "e ha la precedenza su questo campo."
                )
            )

        note = QLabel(
            "La chiave ha effetto subito. Le altre modifiche al prossimo avvio "
            "dell'applicazione."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        for button in buttons.buttons():
            button.setIcon(QIcon())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _api_key_placeholder(self) -> str:
        if packaged_api_key():
            return "Inserisci la tua chiave per usarla al posto di quella inclusa"
        return "senza chiave si usano le medie statiche"

    def _apply_autostart(self) -> bool:
        #Torna False se la finestra deve restare aperta
        if not self._autostart_supported:
            return True
        wanted = self.autostart_check.isChecked()
        wanted_hidden = wanted and self.hidden_start_check.isChecked()
        if wanted == autostart.is_enabled() and wanted_hidden == autostart.starts_hidden():
            return True
        try:
            autostart.set_enabled(wanted, start_hidden=wanted_hidden)
        except autostart.AutostartError as exc:
            QMessageBox.warning(self, "Avvio automatico non modificato", str(exc))
            self.autostart_check.setChecked(autostart.is_enabled())
            self.hidden_start_check.setChecked(autostart.starts_hidden())
            return False
        return True

    def accept(self) -> None:
        if not self._apply_autostart():
            return
        config = self._manager.config
        config.em_zone = self.zone_edit.text().strip() or config.em_zone
        config.country_code = (self.country_edit.text().strip() or config.country_code).upper()
        config.electricitymaps_api_key = self.api_key_edit.text().strip() or None
        self.saved_user_api_key = config.electricitymaps_api_key
        self.api_key_changed = self.saved_user_api_key != self._saved_key
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
