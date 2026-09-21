import logging
from collections.abc import Callable

from PySide6.QtWidgets import QMainWindow, QMenu, QSystemTrayIcon

from ecomonitor.gui.icon import app_icon

#QSystemTrayIcon nativo, nessuna dipendenza aggiuntiva

logger = logging.getLogger(__name__)

TRAY_TOOLTIP = "EcoMonitor"
SHOW_ENTRY = "Apri dashboard"
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
