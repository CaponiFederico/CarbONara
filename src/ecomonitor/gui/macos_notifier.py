from __future__ import annotations

import functools
import logging
import sys
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal, Slot

if TYPE_CHECKING:
    from ecomonitor.notifications import Notification, NotificationKind

#Notifiche native di macOS (UserNotifications, via PyObjC), e il centro vecchio
#(NSUserNotification) quando macOS rifiuta quelle native alla firma ad hoc

logger = logging.getLogger(__name__)

ADVICE_IDENTIFIER = "advice"


def notification_identifier(kind: NotificationKind) -> str:
    from ecomonitor.notifications import OWN_SLOT_KINDS

    return kind.value if kind in OWN_SLOT_KINDS else ADVICE_IDENTIFIER


class NotificationCenter(Protocol):
    on_click: Callable[[], None]

    def post(self, identifier: str, title: str, body: str) -> None: ...

    def remove(self, identifier: str) -> None: ...

    def clear(self) -> None: ...


def _ignore() -> None:
    pass


def _error_text(error: Any) -> str:
    try:
        return str(error.localizedDescription())
    except Exception:
        return str(error)

NOT_ALLOWED_DOMAIN = "UNErrorDomain"
NOT_ALLOWED_CODE = 1


def refused_by_system(granted: bool, error: Any) -> bool:
    if granted or error is None:
        return False
    try:
        return str(error.domain()) == NOT_ALLOWED_DOMAIN and int(error.code()) == NOT_ALLOWED_CODE
    except Exception:
        return False


def _on_posted(error: Any) -> None:
    try:
        if error is not None:
            logger.warning("Notifica non consegnata a macOS (%s)", _error_text(error))
    except Exception:
        logger.exception("Esito della notifica non letto")


@functools.cache
def _delegate_class() -> Any:
    from Foundation import NSObject

    class EcoMonitorNotificationDelegate(NSObject):
        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
            self, center: Any, notification: Any, completion_handler: Any
        ) -> None:
            try:
                completion_handler(self.presentation_options)
            except Exception:
                logger.exception("Notifica non presentata con l'app in primo piano")

        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            self, center: Any, response: Any, completion_handler: Any
        ) -> None:
            try:
                if response.actionIdentifier() == self.default_action:
                    self.on_click()
            except Exception:
                logger.exception("Clic sulla notifica non gestito")
            try:
                completion_handler()
            except Exception:
                logger.exception("Clic sulla notifica non confermato a macOS")

    return EcoMonitorNotificationDelegate


class UserNotificationCenter:
    #Il solo punto che tocca PyObjC

    def __init__(self) -> None:
        if not getattr(sys, "frozen", False):
            #da sorgente non c'e' un bundle a cui macOS possa intestarle
            raise OSError("le notifiche native esistono solo nell'app impacchettata")
        self.on_click: Callable[[], None] = _ignore
        self.refused_by_system = False
        try:
            import UserNotifications as framework

            self._framework: Any = framework
            self._center = framework.UNUserNotificationCenter.currentNotificationCenter()
            self._delegate = _delegate_class().alloc().init()
            self._delegate.presentation_options = (
                framework.UNNotificationPresentationOptionBanner
                | framework.UNNotificationPresentationOptionList
            )
            self._delegate.default_action = framework.UNNotificationDefaultActionIdentifier
            self._delegate.on_click = self._clicked
            self._center.setDelegate_(self._delegate)
            self._center.requestAuthorizationWithOptions_completionHandler_(
                framework.UNAuthorizationOptionAlert | framework.UNAuthorizationOptionSound,
                self._on_authorization,
            )
        except Exception as exc:
            raise OSError(f"centro notifiche di macOS non disponibile ({exc})") from exc

    def _on_authorization(self, granted: bool, error: Any) -> None:
        try:
            if granted:
                logger.info("Notifiche di macOS: autorizzate")
            elif refused_by_system(granted, error):
                self.refused_by_system = True
                logger.info(
                    "macOS rifiuta le notifiche native a questa firma (%s): se c'e', "
                    "si usa il centro notifiche vecchio",
                    _error_text(error),
                )
            else:
                logger.warning(
                    "Le notifiche di CarbO'Nara non sono autorizzate nelle impostazioni di "
                    "macOS%s: non compariranno finche' non vengono riattivate",
                    "" if error is None else f" ({_error_text(error)})",
                )
        except Exception:
            logger.exception("Esito dell'autorizzazione alle notifiche non letto")

    def _clicked(self) -> None:
        self.on_click()

    def post(self, identifier: str, title: str, body: str) -> None:
        framework = self._framework
        try:
            content = framework.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            request = framework.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                identifier, content, None
            )
            self._center.addNotificationRequest_withCompletionHandler_(request, _on_posted)
        except Exception as exc:
            raise OSError(f"notifica non inviata ({exc})") from exc

    def remove(self, identifier: str) -> None:
        try:
            self._center.removeDeliveredNotificationsWithIdentifiers_([identifier])
        except Exception as exc:
            raise OSError(f"notifica non tolta ({exc})") from exc

    def clear(self) -> None:
        try:
            self._center.removeAllDeliveredNotifications()
        except Exception as exc:
            raise OSError(f"notifiche non tolte ({exc})") from exc


@functools.cache
def _legacy_delegate_class() -> Any:
    import objc
    from Foundation import NSObject, NSUserNotificationActivationTypeContentsClicked

    class CarbonaraLegacyNotificationDelegate(  # type: ignore[call-arg]
        NSObject, protocols=[objc.protocolNamed("NSUserNotificationCenterDelegate")]
    ):
        def userNotificationCenter_shouldPresentNotification_(
            self, center: Any, notification: Any
        ) -> bool:
            return True

        def userNotificationCenter_didActivateNotification_(
            self, center: Any, notification: Any
        ) -> None:
            try:
                if notification.activationType() != NSUserNotificationActivationTypeContentsClicked:
                    return
                center.removeDeliveredNotification_(notification)
                self.on_click()
            except Exception:
                logger.exception("Clic sulla notifica non gestito")

    return CarbonaraLegacyNotificationDelegate


class LegacyNotificationCenter:
    def __init__(self) -> None:
        if not getattr(sys, "frozen", False):
            raise OSError("le notifiche native esistono solo nell'app impacchettata")
        self.on_click: Callable[[], None] = _ignore
        self._delivered: dict[str, Any] = {}
        self._serial = 0
        try:
            import Foundation

            self._foundation: Any = Foundation
            self._center = Foundation.NSUserNotificationCenter.defaultUserNotificationCenter()
            if self._center is None:
                raise OSError("nessun bundle a cui intestarle")
            self._delegate = _legacy_delegate_class().alloc().init()
            self._delegate.on_click = self._clicked
            self._center.setDelegate_(self._delegate)
        except Exception as exc:
            raise OSError(f"centro notifiche vecchio di macOS non disponibile ({exc})") from exc

    def _clicked(self) -> None:
        self.on_click()

    def post(self, identifier: str, title: str, body: str) -> None:
        try:
            previous = self._delivered.pop(identifier, None)
            if previous is not None:
                self._center.removeDeliveredNotification_(previous)
            self._serial += 1
            notification = self._foundation.NSUserNotification.alloc().init()
            notification.setIdentifier_(f"{identifier}.{self._serial}")
            notification.setTitle_(title)
            notification.setInformativeText_(body)
            self._center.deliverNotification_(notification)
            self._delivered[identifier] = notification
        except Exception as exc:
            raise OSError(f"notifica non inviata ({exc})") from exc

    def remove(self, identifier: str) -> None:
        previous = self._delivered.pop(identifier, None)
        if previous is None:
            return
        try:
            self._center.removeDeliveredNotification_(previous)
        except Exception as exc:
            raise OSError(f"notifica non tolta ({exc})") from exc

    def clear(self) -> None:
        self._delivered.clear()
        try:
            self._center.removeAllDeliveredNotifications()
        except Exception as exc:
            raise OSError(f"notifiche non tolte ({exc})") from exc


class NativeCenter(NotificationCenter, Protocol):
    refused_by_system: bool


class FallbackNotificationCenter:
    def __init__(self, native: NativeCenter, legacy: NotificationCenter) -> None:
        self._native = native
        self._legacy = legacy
        self._legacy_announced = False
        self._on_click: Callable[[], None] = _ignore

    @property
    def on_click(self) -> Callable[[], None]:
        return self._on_click

    @on_click.setter
    def on_click(self, callback: Callable[[], None]) -> None:
        self._on_click = callback
        self._native.on_click = callback
        self._legacy.on_click = callback

    def _active(self) -> NotificationCenter:
        if not self._native.refused_by_system:
            return self._native
        if not self._legacy_announced:
            self._legacy_announced = True
            logger.info("Notifiche di macOS dal centro vecchio (NSUserNotification)")
        return self._legacy

    def post(self, identifier: str, title: str, body: str) -> None:
        self._active().post(identifier, title, body)

    def remove(self, identifier: str) -> None:
        self._active().remove(identifier)

    def clear(self) -> None:
        failure: OSError | None = None
        for center in (self._native, self._legacy):
            try:
                center.clear()
            except OSError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure


def _click_forwarder(notifier: MacNotifier) -> Callable[[], None]:
    #riferimento debole: il centro non deve tenere vivo il notificatore
    reference = weakref.ref(notifier)

    def forward() -> None:
        target = reference()
        if target is not None:
            target._activated.emit()

    return forward


class MacNotifier(QObject):

    clicked = Signal()
    _activated = Signal()

    def __init__(
        self,
        center: NotificationCenter,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self._center = center
        self._now = now
        self._shown: dict[str, NotificationKind] = {}
        #uno per posto, riusato
        self._expiry_timers: dict[str, QTimer] = {}
        self._activated.connect(self._forward_click, Qt.ConnectionType.QueuedConnection)
        center.on_click = _click_forwarder(self)
        try:
            center.clear()
        except OSError as exc:
            logger.warning("Notifiche di un avvio precedente non tolte (%s)", exc)

    @Slot()
    def _forward_click(self) -> None:
        self.clicked.emit()

    def _stop_expiry(self, identifier: str) -> None:
        timer = self._expiry_timers.get(identifier)
        if timer is not None:
            timer.stop()

    def _start_expiry(self, identifier: str, expires_at: datetime) -> None:
        #macOS non fa scadere una notifica da solo
        timer = self._expiry_timers.get(identifier)
        if timer is None:
            timer = QTimer(self)
            timer.setObjectName(identifier)
            timer.setSingleShot(True)
            timer.timeout.connect(self._on_expired)
            self._expiry_timers[identifier] = timer
        remaining_ms = max(0.0, (expires_at - self._now()).total_seconds() * 1000.0)
        timer.start(int(remaining_ms))

    @Slot()
    def _on_expired(self) -> None:
        timer = self.sender()
        if timer is not None:
            self._remove(timer.objectName())

    def _remove(self, identifier: str) -> None:
        self._stop_expiry(identifier)
        if self._shown.pop(identifier, None) is None:
            return
        try:
            self._center.remove(identifier)
        except OSError as exc:
            logger.debug("Notifica %s non ritirata (%s)", identifier, exc)

    def show(self, notification: Notification) -> None:
        identifier = notification_identifier(notification.kind)
        self._stop_expiry(identifier)
        try:
            self._center.post(identifier, notification.title, notification.text)
        except OSError:
            self._shown.pop(identifier, None)
            raise
        self._shown[identifier] = notification.kind
        if notification.expires_at is not None:
            self._start_expiry(identifier, notification.expires_at)

    def withdraw(self, kind: NotificationKind) -> None:
        identifier = notification_identifier(kind)
        if self._shown.get(identifier) is kind:
            self._remove(identifier)

    def close_all(self) -> None:
        for identifier in list(self._shown):
            self._remove(identifier)


def create_macos_notifier() -> MacNotifier:
    native = UserNotificationCenter()
    center: NotificationCenter = native
    try:
        center = FallbackNotificationCenter(native, LegacyNotificationCenter())
    except OSError as exc:
        logger.warning(
            "%s: se macOS rifiuta le notifiche native, non ne comparira' nessuna", exc
        )
    notifier = MacNotifier(center)
    app = QCoreApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(notifier.close_all)
    return notifier
