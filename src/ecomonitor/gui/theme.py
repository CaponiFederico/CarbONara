from dataclasses import asdict, dataclass
from string import Template

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

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


def build_palette(theme: Theme) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.background,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.card,
        QPalette.ColorRole.AlternateBase: theme.neutral_soft,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.card,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.BrightText: theme.danger,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: theme.card,
        QPalette.ColorRole.PlaceholderText: theme.text_muted,
        QPalette.ColorRole.ToolTipBase: theme.card,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.Link: theme.accent,
        QPalette.ColorRole.Light: theme.card,
        QPalette.ColorRole.Midlight: theme.neutral_soft,
        QPalette.ColorRole.Mid: theme.border,
        QPalette.ColorRole.Dark: theme.border,
        QPalette.ColorRole.Shadow: theme.border,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    #disattivato: il testo si spegne, lo sfondo resta quello del tema
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.text_muted))
    return palette

_MINIMUM_POINT_SIZE = 10

_FONT_SCALE = {
    "font_small": 1.0,
    "font_body": 1.1,
    "font_unit": 1.25,
    "font_title": 1.6,
    "font_card_value": 1.7,
    "font_hero": 2.2,
    "font_advice_title": 2.0,
    "font_advice_second": 1.55,
    "font_advice_detail": 1.4,
    "font_advice_third": 1.25,
}

def system_point_size() -> float:
    app = QApplication.instance()
    size = app.font().pointSizeF() if isinstance(app, QApplication) else QFont().pointSizeF()
    #pointsizeF() = -1 se il font di sistema e' in pixel
    if size <= 0.0:
        return _MINIMUM_POINT_SIZE
    return max(size, _MINIMUM_POINT_SIZE)

def font_sizes(base_point_size: float | None = None) -> dict[str, str]:
    base = (
        system_point_size()
        if base_point_size is None
        else max(base_point_size, _MINIMUM_POINT_SIZE)
    )
    return {name: f"{round(base * factor)}pt" for name, factor in _FONT_SCALE.items()}


_STYLESHEET = Template(
    """
QFrame#root { background: $background; }

QLabel#heroValue { font-size: $font_hero; font-weight: 500; color: $text; }
QLabel#heroUnit { font-size: $font_unit; color: $text_muted; }
QLabel#heroCaption { font-size: $font_small; color: $text_muted; }

QLabel#powerBadge {
    font-size: $font_small;
    padding: 3px 10px;
    border-radius: 9px;
    background: $neutral_soft;
    color: $text_muted;
}
QLabel#powerBadge[state="plugged"] { background: $accent_soft; color: $accent; }
QLabel#powerBadge[state="battery"] { background: $success_soft; color: $success; }

QFrame#metricCard { background: $card; border: 1px solid $border; border-radius: 8px; }
QLabel#cardLabel { font-size: $font_small; color: $text_muted; }
QLabel#cardValue { font-size: $font_card_value; font-weight: 500; color: $text; }
QLabel#cardUnit { font-size: $font_small; color: $text_muted; }

QFrame#adviceBanner { border-radius: 8px; background: $neutral_soft; }
QFrame#adviceBanner QLabel { color: $text_muted; }
QFrame#adviceBanner[state="charge_now"] { background: $success_soft; }
QFrame#adviceBanner[state="charge_now"] QLabel { color: $success; }
QFrame#adviceBanner[state="wait"] { background: $warning_soft; }
QFrame#adviceBanner[state="wait"] QLabel { color: $warning; }
QLabel#adviceTitle { font-size: $font_advice_title; font-weight: 600; }
QLabel#adviceDetail { font-size: $font_advice_detail; }
QLabel#adviceSecond { font-size: $font_advice_second; font-weight: 600; }
QFrame#adviceBanner QLabel#adviceThird { font-size: $font_advice_third; color: $text_muted; }

QLabel#detailKey { font-size: $font_small; color: $text_muted; }
QLabel#detailValue { font-size: $font_small; color: $text; }
QLabel#detailValue[state="estimated"] { color: $warning; }

QStatusBar { background: $background; border-top: 1px solid $border; }
QStatusBar::item { border: none; }
QStatusBar QLabel { font-size: $font_small; color: $text_muted; }

QLabel#statusDot { border-radius: 4px; background: $text_muted; }
QLabel#statusDot[state="live"] { background: $success; }
QLabel#statusDot[state="stale"] { background: $danger; }

QPushButton#settingsButton, QPushButton#navButton {
    font-size: $font_small;
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
    font-size: $font_small;
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

QLabel#pageSubtitle { font-size: $font_small; color: $text_muted; }
QLabel#sectionTitle { font-size: $font_body; font-weight: 500; color: $text; }
QLabel#sectionNote { font-size: $font_small; color: $text_muted; }
QLabel#legendText { font-size: $font_small; color: $text_muted; }
QLabel#emptyState { font-size: $font_body; color: $text_muted; }

/* -- pagine dello studio -- */

QLabel#studyTitle { font-size: $font_title; font-weight: 600; color: $text; }
QLabel#studyText { font-size: $font_body; color: $text; }
QLabel#studyText[state="error"] { color: $warning; }
QLineEdit#studyToken {
    font-size: $font_body;
    padding: 4px 6px;
    border: 1px solid $border;
    border-radius: 6px;
    background: $card;
    color: $text;
}
QLineEdit#studyToken:focus { border-color: $accent; }
"""
)


def build_stylesheet(theme: Theme, base_point_size: float | None = None) -> str:
    values: dict[str, str] = asdict(theme)
    values.update(font_sizes(base_point_size))
    #substitute e non safe_substitute: un segnaposto scritto male deve fallire
    #subito, non produrre un foglio di stile a cui manca una regola in silenzio
    return _STYLESHEET.substitute(values)
