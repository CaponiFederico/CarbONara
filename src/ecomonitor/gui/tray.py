from collections.abc import Callable

from PySide6.QtWidgets import QMainWindow, QMenu, QSystemTrayIcon

from ecomonitor.gui.icon import app_icon

#QSystemTrayIcon nativo, nessuna dipendenza aggiuntiva


def create_tray_icon(window: QMainWindow, on_quit: Callable[[], None]) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(app_icon(), parent=window)  #bug noto di qt
    tray.setToolTip("EcoMonitor")

    def show_window() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    menu = QMenu()
    show_action = menu.addAction("Apri dashboard")
    show_action.triggered.connect(show_window)
    quit_action = menu.addAction("Esci")
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
