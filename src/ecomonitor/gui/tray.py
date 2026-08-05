from collections.abc import Callable

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QMainWindow, QMenu, QSystemTrayIcon

#QSystemTrayIcon nativo, nessuna dipendenza aggiuntiva

def _placeholder_icon() -> QIcon:
    #icona segnaposto a tinta unita, da sostituire con un asset reale
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor("#2e7d32"))
    return QIcon(pixmap)


def create_tray_icon(window: QMainWindow, on_quit: Callable[[], None]) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(_placeholder_icon(), parent=window)
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
