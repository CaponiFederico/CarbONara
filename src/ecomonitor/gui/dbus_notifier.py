from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

if TYPE_CHECKING:
    from ecomonitor.notifications import Notification, NotificationKind

#Notifiche del desktop su Linux 

logger = logging.getLogger(__name__)

SERVICE = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"

APP_NAME = "EcoMonitor"
DESKTOP_ENTRY = "ecomonitor"
ICON_NAME = "ecomonitor"

#l'azione che il desktop invoca cliccando la notifica
DEFAULT_ACTION = "default"
DEFAULT_ACTION_LABEL = "Apri EcoMonitor"

ACTIONS_CAPABILITY = "actions"
MARKUP_CAPABILITY = "body-markup"

SERVER_DEFAULT_TIMEOUT = -1
CALL_TIMEOUT_MS = 1000

ADVICE_SLOT = "advice"

ACTIVATION_TOKEN_ENV_VAR = "XDG_ACTIVATION_TOKEN"

QT_DBUS_LIBRARY = "libQt6DBus.so.6"
APPEND_UINT32_SYMBOL = "_ZN13QDBusArgumentlsEj"
PROCESS_MAPS_PATH = Path("/proc/self/maps")


def notification_slot(kind: NotificationKind) -> str:
    from ecomonitor.notifications import OWN_SLOT_KINDS

    return kind.value if kind in OWN_SLOT_KINDS else ADVICE_SLOT


def body_text(text: str, markup: bool) -> str:
    return escape(text, quote=False) if markup else text


def loaded_library_path(name: str, maps_text: str) -> str | None:
    for line in maps_text.splitlines():
        path = line.partition("/")[2]
        if path and path.rsplit("/", 1)[-1] == name:
            return "/" + path
    return None


class NotificationServer(Protocol):
    capabilities: frozenset[str]
    on_closed: Callable[[int], None]
    on_action: Callable[[int, str], None]
    on_activation_token: Callable[[int, str], None]

    def notify(self, replaces_id: int, title: str, body: str) -> int: ...

    def close(self, notification_id: int) -> None: ...


def _ignore(*_args: object) -> None:
    pass


class SessionBusNotificationServer(QObject):

    def __init__(self) -> None:
        super().__init__()
        from PySide6.QtCore import SLOT
        from PySide6.QtDBus import QDBus, QDBusArgument, QDBusConnection, QDBusMessage

        self._block: Any = QDBus.CallMode.Block
        self._argument_type: Any = QDBusArgument
        self._message_type: Any = QDBusMessage
        self.on_closed: Callable[[int], None] = _ignore
        self.on_action: Callable[[int, str], None] = _ignore
        self.on_activation_token: Callable[[int, str], None] = _ignore

        self._bus = QDBusConnection.sessionBus()
        if not self._bus.isConnected():
            raise OSError("bus di sessione non raggiungibile")
        self._append_uint32 = self._load_uint32_appender()
        self.capabilities = frozenset(self._call("GetCapabilities", [])[0])

        connect: Any = self._bus.connect
        for signal_name, slot in (
            ("NotificationClosed", "_on_closed(QDBusMessage)"),
            ("ActionInvoked", "_on_action(QDBusMessage)"),
            ("ActivationToken", "_on_activation_token(QDBusMessage)"),
        ):
            if not connect(SERVICE, OBJECT_PATH, INTERFACE, signal_name, self, SLOT(slot)):
                raise OSError(f"segnale {signal_name} non collegato")

    def _load_uint32_appender(self) -> Callable[[Any, int], None]:
        import shiboken6

        try:
            path = loaded_library_path(QT_DBUS_LIBRARY, PROCESS_MAPS_PATH.read_text())
        except OSError as exc:
            raise OSError(f"{PROCESS_MAPS_PATH} non leggibile ({exc})") from exc
        if path is None:
            raise OSError(f"{QT_DBUS_LIBRARY} non risulta caricata")
        try:
            function = getattr(ctypes.CDLL(path), APPEND_UINT32_SYMBOL)
        except AttributeError as exc:
            raise OSError(f"{APPEND_UINT32_SYMBOL} non c'e' in {path}") from exc
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        function.restype = ctypes.c_void_p

        def append(argument: Any, value: int) -> None:
            function(shiboken6.getCppPointer(argument)[0], value)

        probe = self._argument_type()
        append(probe, 0)
        if probe.currentSignature() != "u":
            raise OSError(f"uint32 scritto come «{probe.currentSignature()}»")
        return append

    def _uint32(self, value: int) -> Any:
        argument = self._argument_type()
        self._append_uint32(argument, value)
        return argument

    def _call(self, method: str, arguments: list[Any]) -> list[Any]:
        message = self._message_type.createMethodCall(SERVICE, OBJECT_PATH, INTERFACE, method)
        message.setArguments(arguments)
        reply = self._bus.call(message, self._block, CALL_TIMEOUT_MS)
        if reply.type() != self._message_type.MessageType.ReplyMessage:
            raise OSError(f"{method}: {reply.errorMessage() or reply.errorName()}")
        return list(reply.arguments())

    def notify(self, replaces_id: int, title: str, body: str) -> int:
        actions = (
            [DEFAULT_ACTION, DEFAULT_ACTION_LABEL]
            if ACTIONS_CAPABILITY in self.capabilities
            else []
        )
        reply = self._call(
            "Notify",
            [
                APP_NAME,
                self._uint32(replaces_id),
                ICON_NAME,
                title,
                body,
                actions,
                {"desktop-entry": DESKTOP_ENTRY},
                SERVER_DEFAULT_TIMEOUT,
            ],
        )
        return int(reply[0])

    def close(self, notification_id: int) -> None:
        self._call("CloseNotification", [self._uint32(notification_id)])

    @Slot("QDBusMessage")
    def _on_closed(self, message: Any) -> None:
        self.on_closed(int(message.arguments()[0]))

    @Slot("QDBusMessage")
    def _on_action(self, message: Any) -> None:
        notification_id, action = message.arguments()[:2]
        self.on_action(int(notification_id), str(action))

    @Slot("QDBusMessage")
    def _on_activation_token(self, message: Any) -> None:
        notification_id, token = message.arguments()[:2]
        self.on_activation_token(int(notification_id), str(token))


class DBusNotifier(QObject):

    clicked = Signal()

    def __init__(
        self,
        server: NotificationServer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self._server = server
        self._now = now
        #posto → (id dato dal servizio, tipo mostrato)
        self._shown: dict[str, tuple[int, NotificationKind]] = {}
        self._expiry_timers: dict[str, QTimer] = {}
        server.on_closed = self._on_closed
        server.on_action = self._on_action
        server.on_activation_token = self._on_activation_token

    def _slot_of(self, notification_id: int) -> str | None:
        for slot, (shown_id, _kind) in self._shown.items():
            if shown_id == notification_id:
                return slot
        return None

    def _stop_expiry(self, slot: str) -> None:
        timer = self._expiry_timers.pop(slot, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _start_expiry(self, slot: str, expires_at: datetime) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._close(slot))
        remaining_ms = max(0.0, (expires_at - self._now()).total_seconds() * 1000.0)
        timer.start(int(remaining_ms))
        self._expiry_timers[slot] = timer

    def _close(self, slot: str) -> None:
        self._stop_expiry(slot)
        shown = self._shown.pop(slot, None)
        if shown is None:
            return
        try:
            self._server.close(shown[0])
        except OSError as exc:
            logger.debug("Notifica %s non ritirata (%s)", slot, exc)

    def show(self, notification: Notification) -> None:
        slot = notification_slot(notification.kind)
        self._stop_expiry(slot)
        #la nuova prende il posto della vecchia nello stesso posto
        replaces_id = self._shown[slot][0] if slot in self._shown else 0
        markup = MARKUP_CAPABILITY in self._server.capabilities
        try:
            notification_id = self._server.notify(
                replaces_id, notification.title, body_text(notification.text, markup)
            )
        except OSError:
            self._shown.pop(slot, None)
            raise
        self._shown[slot] = (notification_id, notification.kind)
        if notification.expires_at is not None:
            self._start_expiry(slot, notification.expires_at)

    def withdraw(self, kind: NotificationKind) -> None:
        slot = notification_slot(kind)
        shown = self._shown.get(slot)
        if shown is not None and shown[1] is kind:
            self._close(slot)

    def close_all(self) -> None:
        for slot in list(self._shown):
            self._close(slot)

    def _on_closed(self, notification_id: int) -> None:
        slot = self._slot_of(notification_id)
        if slot is not None:
            self._stop_expiry(slot)
            del self._shown[slot]

    def _on_activation_token(self, notification_id: int, token: str) -> None:
        if self._slot_of(notification_id) is not None and token:
            os.environ[ACTIVATION_TOKEN_ENV_VAR] = token

    def _on_action(self, notification_id: int, action: str) -> None:
        if self._slot_of(notification_id) is not None and action == DEFAULT_ACTION:
            self.clicked.emit()


def create_dbus_notifier() -> DBusNotifier:
    notifier = DBusNotifier(SessionBusNotificationServer())
    app = QCoreApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(notifier.close_all)
    return notifier
