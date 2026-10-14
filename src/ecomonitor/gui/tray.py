import logging
from collections.abc import Callable

from PySide6.QtWidgets import QMainWindow, QMenu, QSystemTrayIcon

from ecomonitor.gui.icon import app_icon

#QSystemTrayIcon nativo, nessuna dipendenza aggiuntiva

logger = logging.getLogger(__name__)

TRAY_TOOLTIP = "EcoMonitor"
SHOW_ENTRY = "Apri dashboard"
#fase 1 dello studio
STUDY_SHOW_ENTRY = "Stato dello studio"
STUDY_TOOLTIP = "EcoMonitor — raccolta dati per lo studio"
QUIT_ENTRY = "Esci"


def create_tray_icon(window: QMainWindow, on_quit: Callable[[], None]) -> QSystemTrayIcon:
    def show_window() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    tray = QSystemTrayIcon(app_icon(), parent=window)
    tray.setToolTip(TRAY_TOOLTIP)

    menu = QMenu()
    show_action = menu.addAction(SHOW_ENTRY)
    show_action.triggered.connect(show_window)
    quit_action = menu.addAction(QUIT_ENTRY)
    quit_action.triggered.connect(on_quit)

    tray.setContextMenu(menu)
    #doppio clic: comportamento atteso su Windows e su gran parte dei desktop
    #Linux, dove il menu contestuale non e' scopribile da solo
    tray.activated.connect(
        lambda reason: show_window()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )
    tray.show()
    return tray

def set_study_baseline(tray: QSystemTrayIcon, in_baseline: bool) -> None:
    menu = tray.contextMenu()
    actions = menu.actions() if menu is not None else []
    if actions:
        actions[0].setText(STUDY_SHOW_ENTRY if in_baseline else SHOW_ENTRY)
    tray.setToolTip(STUDY_TOOLTIP if in_baseline else TRAY_TOOLTIP)
