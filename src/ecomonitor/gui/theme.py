from dataclasses import asdict, dataclass
from string import Template

#Tavolozza e foglio di stile dell'applicazione.

@dataclass(frozen=True)
class Theme:
    background: str
    card: str
    border: str
    text: str
    text_muted: str

    accent: str  # informativo: alimentazione da rete, valori neutri in evidenza
    accent_soft: str

    success: str  # buon momento per caricare
    success_soft: str

    warning: str  # conviene aspettare
    warning_soft: str

    danger: str  # campionamento fermo
    neutral_soft: str  # consiglio senza indicazione, o dato non applicabile


LIGHT = Theme(
    background="#f4f4f2",
    card="#ffffff",
    border="#e0e0dc",
    text="#1c1c1a",
    text_muted="#6b6b66",
    accent="#1f5fa8",
    accent_soft="#e7f0fa",
    success="#2e6b31",
    success_soft="#e8f3e8",
    warning="#8a5300",
    warning_soft="#fbf0dd",
    danger="#a62828",
    neutral_soft="#eeeeeb",
)

DARK = Theme(
    background="#1e1e1e",
    card="#2a2a29",
    border="#3b3b39",
    text="#ececea",
    text_muted="#a3a39d",
    accent="#7fb0ea",
    accent_soft="#1d2a3a",
    success="#8ec98f",
    success_soft="#1e2c1e",
    warning="#e5a851",
    warning_soft="#332714",
    danger="#ef7f7f",
    neutral_soft="#333331",
)


def theme_for(is_dark: bool) -> Theme:
    return DARK if is_dark else LIGHT

_STYLESHEET = Template(
    """
QFrame#root { background: $background; }

QLabel#heroValue { font-size: 26pt; font-weight: 500; color: $text; }
QLabel#heroUnit { font-size: 11pt; color: $text_muted; }
QLabel#heroCaption { font-size: 8pt; color: $text_muted; }

QLabel#powerBadge {
    font-size: 8pt;
    padding: 3px 10px;
    border-radius: 9px;
    background: $neutral_soft;
    color: $text_muted;
}
QLabel#powerBadge[state="plugged"] { background: $accent_soft; color: $accent; }
QLabel#powerBadge[state="battery"] { background: $success_soft; color: $success; }

QFrame#metricCard { background: $card; border: 1px solid $border; border-radius: 8px; }
QLabel#cardLabel { font-size: 8pt; color: $text_muted; }
QLabel#cardValue { font-size: 15pt; font-weight: 500; color: $text; }
QLabel#cardUnit { font-size: 8pt; color: $text_muted; }

QFrame#adviceBanner { border-radius: 8px; background: $neutral_soft; }
QFrame#adviceBanner QLabel { color: $text_muted; }
QFrame#adviceBanner[state="charge_now"] { background: $success_soft; }
QFrame#adviceBanner[state="charge_now"] QLabel { color: $success; }
QFrame#adviceBanner[state="wait"] { background: $warning_soft; }
QFrame#adviceBanner[state="wait"] QLabel { color: $warning; }
QLabel#adviceTitle { font-size: 10pt; font-weight: 500; }
QLabel#adviceDetail { font-size: 8pt; }

QLabel#detailKey { font-size: 8pt; color: $text_muted; }
QLabel#detailValue { font-size: 8pt; color: $text; }

QStatusBar { background: $background; border-top: 1px solid $border; }
QStatusBar::item { border: none; }
QStatusBar QLabel { font-size: 8pt; color: $text_muted; }

QLabel#statusDot { border-radius: 4px; background: $text_muted; }
QLabel#statusDot[state="live"] { background: $success; }
QLabel#statusDot[state="stale"] { background: $danger; }

QPushButton#settingsButton, QPushButton#navButton {
    font-size: 8pt;
    padding: 3px 10px;
    border: 1px solid $border;
    border-radius: 6px;
    background: $card;
    color: $text;
}
QPushButton#settingsButton:hover, QPushButton#navButton:hover { border-color: $text_muted; }

/* -- pagina di storico -- */

QFrame#historyRoot { background: $background; }

QPushButton#periodButton {
    font-size: 8pt;
    padding: 4px 14px;
    border: 1px solid $border;
    border-radius: 6px;
    background: $card;
    color: $text_muted;
}
QPushButton#periodButton:hover { border-color: $text_muted; }
QPushButton#periodButton:checked {
    background: $accent_soft;
    border-color: $accent;
    color: $accent;
    font-weight: 500;
}

QLabel#pageSubtitle { font-size: 8pt; color: $text_muted; }
QLabel#sectionTitle { font-size: 9pt; font-weight: 500; color: $text; }
QLabel#sectionNote { font-size: 8pt; color: $text_muted; }
QLabel#legendText { font-size: 8pt; color: $text_muted; }
QLabel#emptyState { font-size: 9pt; color: $text_muted; }
"""
)


def build_stylesheet(theme: Theme) -> str:
    #substitute e non safe_substitute: un segnaposto scritto male deve fallire
    #subito, non produrre un foglio di stile a cui manca una regola in silenzio
    return _STYLESHEET.substitute(asdict(theme))
