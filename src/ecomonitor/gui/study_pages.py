import threading
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ecomonitor.config import STUDY_TOKEN_PATTERN, TOKEN_DIGEST_PATTERN
from ecomonitor.study_phase import StudyDates

#Le due pagine dello studio
#fase 1: l'app raccoglie e non mostra consumi ne' consigli

_MONTHS = (
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)  # fmt: skip

STUDY_TITLE = "Partecipi allo studio CarbO'Nara"
ONBOARDING_TITLE = "Benvenuto in CarbO'Nara"
JOIN_TEXT = "Partecipa"
VERIFYING_TEXT = "Verifica del codice…"
NO_SERVER_TEXT = (
    "Questa copia di CarbO'Nara non conosce l'indirizzo del server dello studio, "
    "quindi non può verificare il codice."
)


def verify_study_token(endpoint: str, token: str) -> StudyDates:
    #il server riconosce il codice e fissa il calendario
    from ecomonitor.study_upload import start_study

    return start_study(endpoint, token)


def run_in_background(work: Callable[[], None]) -> None:
    #la verifica e' una chiamata di rete
    threading.Thread(target=work, name="study-start", daemon=True).start()


def local_date_text(moment: datetime) -> str:
    #i nomi dei mesi scritti qui: non dipendono dalla lingua del sistema
    local = moment.astimezone()
    return f"{local.day} {_MONTHS[local.month - 1]} alle {local:%H:%M}"


def baseline_text(advice_starts_at: datetime | None) -> str:
    when = (
        f"Fino al {local_date_text(advice_starts_at)}"
        if advice_starts_at is not None
        else "Per la prima settimana"
    )
    return (
        f"{when} CarbO'Nara raccoglie i dati in silenzio: non mostra consumi né "
        "consigli, e non serve fare niente. Poi si aprirà da solo con tutte le sue "
        "funzioni.\n\nPuoi chiudere questa finestra: la raccolta continua."
    )


def study_token_problem(text: str) -> str | None:
    token = text.strip()
    if not token:
        return "Scrivi il codice che ti è stato consegnato."
    if TOKEN_DIGEST_PATTERN.fullmatch(token):
        return "Questa è l'impronta del codice, non il codice: usa quello consegnato."
    if STUDY_TOKEN_PATTERN.fullmatch(token) is None:
        return (
            "Il codice contiene solo lettere, cifre, - e _, senza spazi: controlla di "
            "averlo copiato per intero."
        )
    return None


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("studyTitle")
    label.setWordWrap(True)
    return label


def _body(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("studyText")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return label


class StudyPhasePage(QFrame):
    #fase 1: niente consumi ne' consigli, solo che cosa sta succedendo
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("root")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(14)

        self.title_label = _title(STUDY_TITLE)
        self.text_label = _body(baseline_text(None))

        self.settings_button = QPushButton("Impostazioni…")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self.settings_requested)
        self.quit_button = QPushButton("Esci")
        self.quit_button.setObjectName("navButton")
        self.quit_button.setToolTip("Ferma la raccolta fino al prossimo avvio")
        self.quit_button.clicked.connect(self.quit_requested)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.settings_button)
        buttons.addWidget(self.quit_button)

        layout.addWidget(self.title_label)
        layout.addWidget(self.text_label)
        layout.addStretch(1)
        layout.addLayout(buttons)

    def set_advice_start(self, advice_starts_at: datetime | None) -> None:
        self.text_label.setText(baseline_text(advice_starts_at))


class OnboardingPage(QFrame):
    joined = Signal(str)
    skipped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("root")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(14)

        self.token_edit = QLineEdit()
        self.token_edit.setObjectName("studyToken")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("codice dello studio")
        self.token_edit.setAccessibleName("Codice dello studio")
        self.token_edit.returnPressed.connect(self._join)
        self.token_edit.textChanged.connect(self._hide_error)

        self.error_label = _body("")
        _set_error(self.error_label)
        self.error_label.setVisible(False)

        self.join_button = QPushButton(JOIN_TEXT)
        self.join_button.setObjectName("settingsButton")
        self.join_button.setDefault(True)
        self.join_button.clicked.connect(self._join)
        self.skip_button = QPushButton("Usa l'app senza partecipare")
        self.skip_button.setObjectName("navButton")
        self.skip_button.clicked.connect(self.skipped)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.join_button)

        layout.addWidget(_title(ONBOARDING_TITLE))
        layout.addWidget(
            _body(
                "Se partecipi allo studio dell'Università di Pisa, scrivi qui il codice "
                "che ti è stato consegnato. Altrimenti puoi usare l'app normalmente."
            )
        )
        layout.addWidget(self.token_edit)
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        layout.addLayout(buttons)

    def _join(self) -> None:
        problem = study_token_problem(self.token_edit.text())
        if problem is not None:
            self.error_label.setText(problem)
            self.error_label.setVisible(True)
            return
        self.joined.emit(self.token_edit.text().strip())

    def show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)

    def set_busy(self, busy: bool) -> None:
        self.token_edit.setEnabled(not busy)
        self.join_button.setEnabled(not busy)
        self.skip_button.setEnabled(not busy)
        self.join_button.setText(VERIFYING_TEXT if busy else JOIN_TEXT)

    def _hide_error(self, _text: str) -> None:
        self.error_label.setVisible(False)


def _set_error(label: QLabel) -> None:
    #il colore di avviso del tema
    label.setProperty("state", "error")
