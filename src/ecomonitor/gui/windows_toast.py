from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from xml.sax.saxutils import escape, quoteattr

from ecomonitor.show_window import PIPE_FLAG

if TYPE_CHECKING:
    from ecomonitor.notifications import Notification, NotificationKind

#Notifiche native di Windows (WinRT)

logger = logging.getLogger(__name__)

#lo stesso identificativo del pacchetto macOS e dell'installer
APP_USER_MODEL_ID = "io.github.caponifederico.carbonara"
APP_DISPLAY_NAME = "CarbO'Nara"

URI_SCHEME = "ecomonitor"
ACTIVATION_URI = f"{URI_SCHEME}:show"

TOAST_GROUP = "ecomonitor"
ADVICE_TAG = "advice"

#accanto a CarbONara.exe nel pacchetto
SHOW_WINDOW_EXECUTABLE = "CarbONaraShow.exe"
ICON_FILE_NAME = "notification_icon.png"
ICON_SIZE = 256

CLASSES_KEY = r"Software\Classes"
IDENTITY_KEY = rf"{CLASSES_KEY}\AppUserModelId\{APP_USER_MODEL_ID}"
PROTOCOL_KEY = rf"{CLASSES_KEY}\{URI_SCHEME}"


def activation_command(
    pipe_name: str, executable: Path | None = None, frozen: bool | None = None
) -> str:
    #La riga che Windows esegue al clic
    executable = executable or Path(sys.executable)
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        program = f'"{executable.with_name(SHOW_WINDOW_EXECUTABLE)}"'
    else:
        #da sorgente: pythonw, che non apre una console
        program = f'"{executable.with_name("pythonw.exe")}" -m ecomonitor.show_window'
    return f'{program} {PIPE_FLAG} {pipe_name} "%1"'


def identity_registry_values(icon_path: Path, command: str) -> list[tuple[str, str, str]]:
    return [
        #senza questa chiave Windows non conosce l'applicazione e scarta le notifiche
        (IDENTITY_KEY, "DisplayName", APP_DISPLAY_NAME),
        (IDENTITY_KEY, "IconUri", str(icon_path)),
        (PROTOCOL_KEY, "", f"URL:{APP_DISPLAY_NAME}"),
        (PROTOCOL_KEY, "URL Protocol", ""),
        (rf"{PROTOCOL_KEY}\shell\open\command", "", command),
    ]


def register_identity(icon_path: Path, command: str) -> None:
    if sys.platform == "win32":
        import winreg

        for subkey, name, data in identity_registry_values(icon_path, command):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
    else:
        raise OSError("le notifiche native esistono solo su Windows")


def write_icon(path: Path) -> None:
    from ecomonitor.gui.icon import app_icon

    path.parent.mkdir(parents=True, exist_ok=True)
    if not app_icon().pixmap(ICON_SIZE, ICON_SIZE).save(str(path), "PNG"):
        raise OSError(f"icona delle notifiche non scritta in {path}")


def toast_xml(title: str, text: str) -> str:
    first, *rest = text.split("\n")
    lines = [title, first, *([" ".join(rest)] if rest else [])]
    return (
        f'<toast activationType="protocol" launch={quoteattr(ACTIVATION_URI)}>'
        '<visual><binding template="ToastGeneric">'
        + "".join(f"<text>{escape(line)}</text>" for line in lines)
        + "</binding></visual></toast>"
    )


def toast_tag(kind: NotificationKind) -> str:
    from ecomonitor.notifications import OWN_SLOT_KINDS

    return kind.value if kind in OWN_SLOT_KINDS else ADVICE_TAG


class ToastPlatform(Protocol):
    def show(self, xml: str, tag: str, expires_at: datetime | None) -> None: ...

    def remove(self, tag: str) -> None: ...

    def clear(self) -> None: ...


class WinRtToastPlatform:
    #Il solo punto che tocca WinRT

    def __init__(self, app_id: str = APP_USER_MODEL_ID) -> None:
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import (
            NotificationSetting,
            ToastNotification,
            ToastNotificationManager,
        )

        self._app_id = app_id
        self._document_type: Any = XmlDocument
        self._toast_type: Any = ToastNotification
        self._notifier = ToastNotificationManager.create_toast_notifier_with_id(app_id)
        self._history = ToastNotificationManager.history

        setting = self._notifier.setting
        if setting != NotificationSetting.ENABLED:
            logger.warning(
                "Le notifiche di CarbO'Nara sono disattivate nelle impostazioni di "
                "Windows (%s): non compariranno finche' non vengono riattivate",
                setting.name,
            )

    def show(self, xml: str, tag: str, expires_at: datetime | None) -> None:
        document = self._document_type()
        document.load_xml(xml)
        toast = self._toast_type(document)
        toast.tag = tag
        toast.group = TOAST_GROUP
        if expires_at is not None:
            toast.expiration_time = expires_at
        self._notifier.show(toast)

    def remove(self, tag: str) -> None:
        self._history.remove_grouped_tag_with_id(tag, TOAST_GROUP, self._app_id)

    def clear(self) -> None:
        self._history.clear_with_id(self._app_id)


class WindowsToastNotifier:
    def __init__(self, toast_platform: ToastPlatform) -> None:
        self._platform = toast_platform
        self._shown: dict[str, NotificationKind] = {}
        try:
            self._platform.clear()
        except OSError as exc:
            logger.warning("Notifiche di un avvio precedente non tolte (%s)", exc)

    def _remove(self, tag: str) -> None:
        if self._shown.pop(tag, None) is None:
            return
        try:
            self._platform.remove(tag)
        except OSError as exc:
            logger.debug("Notifica %s non ritirata (%s)", tag, exc)

    def show(self, notification: Notification) -> None:
        tag = toast_tag(notification.kind)
        self._remove(tag)
        self._platform.show(
            toast_xml(notification.title, notification.text), tag, notification.expires_at
        )
        self._shown[tag] = notification.kind

    def withdraw(self, kind: NotificationKind) -> None:
        tag = toast_tag(kind)
        if self._shown.get(tag) is kind:
            self._remove(tag)


def create_windows_notifier(data_path: Path, pipe_name: str) -> WindowsToastNotifier:
    icon_path = data_path / ICON_FILE_NAME
    write_icon(icon_path)
    register_identity(icon_path, activation_command(pipe_name))
    return WindowsToastNotifier(WinRtToastPlatform())
