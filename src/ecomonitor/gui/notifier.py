from __future__ import annotations

import logging
import platform
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QSystemTrayIcon

if TYPE_CHECKING:
    from ecomonitor.notifications import Notification, NotificationKind

#Consegna di una notifica al sistema

logger = logging.getLogger(__name__)

MESSAGE_TIMEOUT_MS = 10_000


class Notifier(Protocol):
    def show(self, notification: Notification) -> None: ...

    def withdraw(self, kind: NotificationKind) -> None: ...


class TrayNotifier(QObject):

    clicked = Signal()

    def __init__(self, tray: QSystemTrayIcon, system: str | None = None) -> None:
        super().__init__(tray)
        self._tray = tray
        self._can_clear = (system or platform.system()) == "Windows"
        self._shown_kind: NotificationKind | None = None
        tray.messageClicked.connect(self.clicked)

    def _clear(self) -> None:
        if self._can_clear and self._shown_kind is not None:
            self._tray.showMessage("", "", QSystemTrayIcon.MessageIcon.NoIcon, 0)
        self._shown_kind = None

    def show(self, notification: Notification) -> None:
        self._clear()
        self._shown_kind = notification.kind
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if notification.is_problem
            else QSystemTrayIcon.MessageIcon.Information
        )
        self._tray.showMessage(notification.title, notification.text, icon, MESSAGE_TIMEOUT_MS)

    def withdraw(self, kind: NotificationKind) -> None:
        if self._shown_kind is kind:
            self._clear()


def _create_windows_notifier(data_path: Path, pipe_name: str) -> Notifier | None:
    try:
        from ecomonitor.gui.windows_toast import create_windows_notifier

        return create_windows_notifier(data_path, pipe_name)
    except Exception as exc:
        logger.warning(
            "Notifiche native di Windows non disponibili (%s): si usano quelle "
            "dell'area di notifica, che non restano nel centro notifiche",
            exc,
        )
        return None


def create_notifier(
    tray: QSystemTrayIcon | None,
    on_click: Callable[[], None],
    data_path: Path | None = None,
    pipe_name: str | None = None,
    system: str | None = None,
) -> Notifier | None:
    system = system or platform.system()
    if system == "Windows" and data_path is not None and pipe_name is not None:
        native = _create_windows_notifier(data_path, pipe_name)
        if native is not None:
            return native

    if tray is None or not tray.supportsMessages():
        logger.info(
            "Notifiche di sistema non disponibili su questo desktop: consigli e "
            "problemi restano visibili solo nella finestra"
        )
        return None
    notifier = TrayNotifier(tray, system)
    notifier.clicked.connect(on_click)
    return notifier
