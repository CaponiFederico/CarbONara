import logging
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from PySide6.QtCore import QCoreApplication, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)

from ecomonitor import autostart
from ecomonitor.config import (
    ATTRIBUTION_MODELS,
    STUDY_TOKEN_PATTERN,
    TOKEN_DIGEST_PATTERN,
    ConfigManager,
)
from ecomonitor.gui import study_pages
from ecomonitor.gui.theme import LIGHT, Theme, build_palette
from ecomonitor.gui.zone_map import ZoneMapWidget, country_of_zone, normalize_country
from ecomonitor.gui.zone_map_data import COUNTRY_NAMES, COUNTRY_ZONES, ZONE_NAMES, ZONE_REGIONS
from ecomonitor.study_phase import StudyDates, StudyPhase, StudyStartFailed

logger = logging.getLogger(__name__)


#La configurazione vive in un JSON nella cartella utente

#la lista viene da config.ATTRIBUTION_MODELS
_ATTRIBUTION_MODELS = ATTRIBUTION_MODELS

#l'impronta SHA-256 del token
_TOKEN_DIGEST_PATTERN = TOKEN_DIGEST_PATTERN

CHARGE_ADVICE_LABEL = "Consigli sulla ricarica"
WORKLOAD_ADVICE_LABEL = "Consigli sul momento migliore per le attività pesanti"
PROBLEM_NOTIFICATIONS_LABEL = "Problemi di misura e di token"
ADVICE_TOOLTIP = "Poche al giorno, solo con la finestra chiusa e solo da previsione di rete"
STUDY_ADVICE_TOOLTIP = "Nella prima fase dello studio i consigli restano spenti"


def _has_battery() -> bool:
    #l'import sta qui
    from ecomonitor.power_context import read_power_source_state

    return read_power_source_state().battery_percent is not None


#spazio fra il bordo della cartina e l'elenco delle regioni 
_ZONE_LABEL_GAP = 4
class WrappedTextBox(QWidget):
    #etichetta che va a capo
    def __init__(self, label: QLabel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(label)
        self._reserved_texts: tuple[str, ...] = ()

    def set_reserved_texts(self, texts: Iterable[str]) -> None:
        self._reserved_texts = tuple(texts)
        self._reserve_space()

    def _reserve_space(self) -> None:
        width = self._label.width()
        if width <= 0:
            return
        metrics = self._label.fontMetrics()
        tallest = max(
            metrics.boundingRect(
                QRect(0, 0, width, 0), int(Qt.TextFlag.TextWordWrap), text
            ).height()
            for text in (self._reserved_texts or (self._label.text(),))
        )
        if tallest != self.minimumHeight():
            self.setMinimumHeight(tallest)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reserve_space()


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        manager: ConfigManager | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("CarbO'Nara — impostazioni")
        self._manager = manager or ConfigManager()
        config = self._manager.config

        self._saved_zone = config.em_zone
        self.saved_zone = config.em_zone
        self._saved_country = normalize_country(config.country_code)
        self.saved_country = self._saved_country
        self.zone_changed = False
        #il paese scritto nel campo decide quale cartina si vede
        self.country_edit = QLineEdit(config.country_code)
        self.zone_map = ZoneMapWidget(
            config.em_zone, theme=theme or LIGHT, country=config.country_code
        )
        self.zone_map.zone_changed.connect(self._on_zone_picked)
        self.zone_label = QLabel()
        self.zone_label.setWordWrap(True)
        self.zone_caption = WrappedTextBox(self.zone_label)
        self._refresh_zone_label()
        self.country_edit.textChanged.connect(self._on_country_typed)

        form = QFormLayout()

        form.addRow("Codice paese (ISO):", self.country_edit)

        #il token del partecipante allo studio: e' suo, quindi si rilegge e si cancella
        self._saved_study_token = config.study_token
        self._saved_study_phase = config.study_phase(datetime.now(UTC))
        self.saved_study_token = self._saved_study_token
        self.study_token_changed = False
        self._verified_study_dates: StudyDates | None = None
        self.saved_study_dates: StudyDates | None = None
        self.study_token_edit = QLineEdit(self._saved_study_token or "")
        self.study_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.study_token_edit.setPlaceholderText(
            "Inserisci il token ricevuto all'adesione allo studio"
        )
        self.study_token_edit.setToolTip(
            "Token personale dello studio di tesi. Ogni ora si inviano i consumi orari "
            "successivi all'adesione. Per smettere di inviare: cancella il campo e salva."
        )
        form.addRow("Token dello studio:", self.study_token_edit)

        self.attribution_combo = QComboBox()
        self.attribution_combo.addItems(_ATTRIBUTION_MODELS)
        if config.attribution_model in _ATTRIBUTION_MODELS:
            self.attribution_combo.setCurrentText(config.attribution_model)
        form.addRow("Modello di attribuzione:", self.attribution_combo)

        self.autostart_check = QCheckBox("Avvia CarbO'Nara all'accesso")
        self._autostart_supported = autostart.is_supported()
        #letti una volta: su Windows ognuno e' un processo schtasks sul thread
        #della finestra, e al Save si confronta con questi invece di rileggerli
        self._autostart_enabled = self._autostart_supported and autostart.is_enabled()
        self._autostart_hidden = self._autostart_supported and autostart.starts_hidden()
        if self._autostart_supported:
            self.autostart_check.setChecked(self._autostart_enabled)
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
            self.hidden_start_check.setChecked(self._autostart_hidden)
            #senza avvio automatico non c'e' niente da far partire nascosto
            self.hidden_start_check.setEnabled(self.autostart_check.isChecked())
            self.autostart_check.toggled.connect(self.hidden_start_check.setEnabled)
            self.hidden_start_check.setToolTip(
                "Vale solo all'accesso, se CarbO'Nara parte automaticamente"
            )
        else:
            self.hidden_start_check.setChecked(False)
            self.hidden_start_check.setEnabled(False)
            self.hidden_start_check.setToolTip(autostart.unsupported_reason())
        form.addRow("", self.hidden_start_check)

        self._saved_advice_notifications = config.advice_notifications_enabled
        self.saved_advice_notifications = self._saved_advice_notifications
        self._saved_problem_notifications = config.problem_notifications_enabled
        self.saved_problem_notifications = self._saved_problem_notifications
        self.notifications_changed = False
        #il consiglio che si riceve dipende dal dispositivo
        self.advice_notifications_check = QCheckBox(
            CHARGE_ADVICE_LABEL if _has_battery() else WORKLOAD_ADVICE_LABEL
        )
        self.advice_notifications_check.setChecked(self._saved_advice_notifications)
        self.study_token_edit.textChanged.connect(self._refresh_advice_notifications)
        self._refresh_advice_notifications()
        form.addRow("Notifiche:", self.advice_notifications_check)
        self.problem_notifications_check = QCheckBox(PROBLEM_NOTIFICATIONS_LABEL)
        self.problem_notifications_check.setChecked(self._saved_problem_notifications)
        form.addRow("", self.problem_notifications_check)

        layout = QVBoxLayout(self)
        title = QLabel("Zona di mercato elettrico")
        layout.addWidget(title)
        layout.addWidget(self.zone_map, stretch=1)
        layout.addSpacing(_ZONE_LABEL_GAP)
        layout.addWidget(self.zone_caption)
        layout.addLayout(form)

        note = QLabel(
            "Paese, zona, token e notifiche hanno effetto subito. Le altre "
            "modifiche al prossimo avvio dell'applicazione."
        )
        note.setWordWrap(True)
        self.note_box = WrappedTextBox(note)
        layout.addWidget(self.note_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        for button in buttons.buttons():
            button.setIcon(QIcon())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._apply_theme(theme or LIGHT)

    def _apply_theme(self, theme: Theme) -> None:
        #I colori li decide il tema dell'app
        theme_is_dark = QColor(theme.background).lightness() < 128
        style_is_dark = self.style().standardPalette().window().color().lightness() < 128
        if theme_is_dark != style_is_dark:
            fusion = QStyleFactory.create("Fusion")
            if fusion is not None:
                #Qt non ne prende possesso: senza un riferimento verrebbe raccolto
                self._fusion_style = fusion
                for widget in [self, *self.findChildren(QWidget)]:
                    widget.setStyle(fusion)
        self.setPalette(build_palette(theme))

    def _on_zone_picked(self, zone: str) -> None:
        #la scelta si vede subito, ma vale solo dal Save in poi
        self._refresh_zone_label()

    def _on_country_typed(self, text: str) -> None:
        self.zone_map.set_country(text)
        self._refresh_zone_label()

    @staticmethod
    def _zone_description(zone: str) -> str:
        name = ZONE_NAMES.get(zone)
        if name is None:
            return (
                f"Zona configurata: {zone} — non e' fra le zone della cartina, "
                "scegline una per sostituirla."
            )
        regions = ZONE_REGIONS.get(zone)
        return f"{name} ({zone}) — {regions}" if regions else f"{name} ({zone})"

    def _caption_text(self) -> str:
        zone = self.zone_map.selected_zone
        if zone is not None:
            return self._zone_description(zone)
        country = self.zone_map.country
        if country is None:
            return f"{self.zone_map.empty_message()} Resta la zona {self._saved_zone}."
        if country_of_zone(self._saved_zone) is None:
            return self._zone_description(self._saved_zone)
        return f"{COUNTRY_NAMES[country]}: scegli la zona sulla cartina."

    def _refresh_zone_label(self) -> None:
        self.zone_label.setText(self._caption_text())
        #lo spazio lo detta il testo piu' alto fra quelli del paese mostrato
        texts = [self._zone_description(zone) for zone in self.zone_map.zones]
        texts.append(self.zone_label.text())
        self.zone_caption.set_reserved_texts(texts)

    def _refresh_advice_notifications(self) -> None:
        #un token nuovo riparte dalla fase 1
        token = self.study_token_edit.text().strip()
        if not token:
            in_baseline = False
        elif token != self._saved_study_token:
            in_baseline = True
        else:
            in_baseline = self._saved_study_phase is StudyPhase.BASELINE
        self.advice_notifications_check.setEnabled(not in_baseline)
        self.advice_notifications_check.setToolTip(
            STUDY_ADVICE_TOOLTIP if in_baseline else ADVICE_TOOLTIP
        )

    def _apply_autostart(self) -> bool:
        #Torna False se la finestra deve restare aperta
        if not self._autostart_supported:
            return True
        wanted = self.autostart_check.isChecked()
        wanted_hidden = wanted and self.hidden_start_check.isChecked()
        if wanted == self._autostart_enabled and wanted_hidden == self._autostart_hidden:
            return True
        try:
            self._run_without_freezing(
                lambda: autostart.set_enabled(wanted, start_hidden=wanted_hidden)
            )
        except autostart.AutostartError as exc:
            QMessageBox.warning(self, "Avvio automatico non modificato", str(exc))
            #qui si rilegge: la voce puo' essere rimasta a meta'
            self._autostart_enabled = autostart.is_enabled()
            self._autostart_hidden = autostart.starts_hidden()
            self.autostart_check.setChecked(self._autostart_enabled)
            self.hidden_start_check.setChecked(self._autostart_hidden)
            return False
        self._autostart_enabled, self._autostart_hidden = wanted, wanted_hidden
        return True

    def _run_without_freezing(self, work: Callable[[], None]) -> None:
        outcome: list[BaseException] = []
        done = threading.Event()

        def job() -> None:
            try:
                work()
            except BaseException as exc:
                outcome.append(exc)
            finally:
                done.set()

        self.setEnabled(False)
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            threading.Thread(target=job, name="autostart", daemon=True).start()
            while not done.wait(0.02):
                QCoreApplication.processEvents()
        finally:
            QGuiApplication.restoreOverrideCursor()
            self.setEnabled(True)
        if outcome:
            error = outcome.pop()
            try:
                raise error
            finally:
                del error

    def _check_study_token(self) -> bool:
        token = self.study_token_edit.text().strip()
        if not token:
            return True
        if _TOKEN_DIGEST_PATTERN.fullmatch(token):
            QMessageBox.warning(
                self,
                "Questo non è il token",
                "Hai incollato l'impronta SHA-256, non il token: usa la stringa che ti è "
                "stata consegnata all'adesione.",
            )
            return False
        if STUDY_TOKEN_PATTERN.fullmatch(token) is None:
            QMessageBox.warning(
                self,
                "Token dello studio non valido",
                "Il token contiene solo lettere, cifre, - e _, senza spazi: controlla di "
                "averlo incollato per intero.",
            )
            return False
        if token == self._saved_study_token:
            return True
        return self._verify_study_token(token)

    def _verify_study_token(self, token: str) -> bool:
        #un token nuovo si salva solo se il server lo riconosce
        endpoint = self._manager.config.effective_study_endpoint
        if endpoint is None:
            QMessageBox.warning(self, "Token dello studio non verificato", study_pages.NO_SERVER_TEXT)
            return False
        verify = study_pages.verify_study_token
        verified: list[StudyDates] = []
        try:
            self._run_without_freezing(lambda: verified.append(verify(endpoint, token)))
        except StudyStartFailed as exc:
            QMessageBox.warning(self, "Token dello studio non verificato", str(exc))
            return False
        except Exception as exc:
            logger.error("Verifica del token dello studio non riuscita: %r", exc)
            QMessageBox.warning(
                self, "Token dello studio non verificato", f"Verifica non riuscita: {exc}"
            )
            return False
        self._verified_study_dates = verified[0]
        return True

    def accept(self) -> None:
        country = normalize_country(self.country_edit.text()) or self._saved_country
        if country != self._saved_country and country not in COUNTRY_ZONES:
            #senza cartina non si sceglie la zona: resterebbe quella di prima,
            #e paese nuovo con zona vecchia misurerebbero la rete sbagliata
            QMessageBox.information(
                self,
                "Paese senza cartina",
                f"Per «{country}» non c'è la cartina delle zone, quindi non si può "
                "scegliere una zona di quel paese. Scrivi il codice di un paese europeo "
                f"oppure lascia {self._saved_country}.",
            )
            return
        if (
            country != self._saved_country
            and country in COUNTRY_ZONES
            and self.zone_map.selected_zone is None
        ):
            QMessageBox.information(
                self,
                "Scegli la zona",
                f"Scegli sulla cartina la zona di {COUNTRY_NAMES[country]} in cui si trova "
                "il computer: senza, resterebbe la zona di prima.",
            )
            return
        if not self._check_study_token():
            return
        if not self._apply_autostart():
            return
        config = self._manager.config
        config.em_zone = self.zone_map.selected_zone or config.em_zone
        self.saved_zone = config.em_zone
        config.country_code = country
        self.saved_country = country
        self.zone_changed = self.saved_zone != self._saved_zone or country != self._saved_country
        config.study_token = self.study_token_edit.text().strip() or None
        self.saved_study_token = config.study_token
        self.study_token_changed = self.saved_study_token != self._saved_study_token
        if self.study_token_changed:
            config.reset_study_calendar()
            if self.saved_study_token is not None and self._verified_study_dates is not None:
                config.apply_study_dates(self._verified_study_dates)
        self.saved_study_dates = (
            self._verified_study_dates if self.saved_study_token is not None else None
        )
        config.attribution_model = self.attribution_combo.currentText()
        config.advice_notifications_enabled = self.advice_notifications_check.isChecked()
        self.saved_advice_notifications = config.advice_notifications_enabled
        config.problem_notifications_enabled = self.problem_notifications_check.isChecked()
        self.saved_problem_notifications = config.problem_notifications_enabled
        self.notifications_changed = (
            self.saved_advice_notifications != self._saved_advice_notifications
            or self.saved_problem_notifications != self._saved_problem_notifications
        )
        try:
            self._manager.save()
        except OSError as exc:
            self.zone_changed = False
            self.study_token_changed = False
            self.notifications_changed = False
            QMessageBox.warning(
                self,
                "Impossibile salvare le impostazioni",
                f"Le modifiche non sono state salvate: {exc}",
            )
            return
        super().accept()
