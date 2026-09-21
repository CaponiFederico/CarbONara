import ctypes
import ctypes.util
import logging
import platform

#icona nel doc macOS deve accendersi e spegnersi con la finestra

logger = logging.getLogger(__name__)

REGULAR = 0 #icona nel dock e menu in alto
ACCESSORY = 1 #resta solo la barra

_runtime: "_ObjectiveCRuntime | None" = None
_runtime_loaded = False


class _ObjectiveCRuntime:
    #chiamate di NSApplication
    def __init__(self, objc: ctypes.CDLL) -> None:
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._objc = objc

        address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        if address is None:
            raise OSError("objc_msgSend non ha un indirizzo")
        self._send_object = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(address)
        self._send_policy = ctypes.CFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long
        )(address)
        self._read_policy = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p
        )(address)
        self._send_activate = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool
        )(address)

    def _selector(self, name: bytes) -> ctypes.c_void_p:
        return ctypes.c_void_p(self._objc.sel_registerName(name))

    def shared_application(self) -> ctypes.c_void_p | None:
        klass = self._objc.objc_getClass(b"NSApplication")
        if not klass:
            return None
        app = self._send_object(ctypes.c_void_p(klass), self._selector(b"sharedApplication"))
        return ctypes.c_void_p(app) if app else None

    def activation_policy(self, app: ctypes.c_void_p) -> int:
        return int(self._read_policy(app, self._selector(b"activationPolicy")))

    def set_activation_policy(self, app: ctypes.c_void_p, policy: int) -> bool:
        return bool(self._send_policy(app, self._selector(b"setActivationPolicy:"), policy))

    def activate(self, app: ctypes.c_void_p) -> None:
        #tornando da Accessory la finestra non viene davanti da sola
        self._send_activate(app, self._selector(b"activateIgnoringOtherApps:"), True)


def _load_runtime() -> "_ObjectiveCRuntime | None":
    #una volta sola
    global _runtime, _runtime_loaded
    if _runtime_loaded:
        return _runtime
    _runtime_loaded = True

    objc_path = ctypes.util.find_library("objc")
    appkit_path = ctypes.util.find_library("AppKit")
    if objc_path is None or appkit_path is None:
        logger.debug("Runtime Objective-C non trovato: l'icona nel Dock resta com'e'")
        return None
    try:
        #AppKit 
        ctypes.cdll.LoadLibrary(appkit_path)
        _runtime = _ObjectiveCRuntime(ctypes.cdll.LoadLibrary(objc_path))
    except OSError as error:
        logger.debug("Runtime Objective-C non caricabile (%s)", error)
        _runtime = None
    return _runtime


def dock_icon_supported(system: str | None = None) -> bool:
    #Il Dock e' solo di macOS
    return (system or platform.system()) == "Darwin"


def set_dock_icon_visible(visible: bool) -> bool:
    if not dock_icon_supported():
        return False
    runtime = _load_runtime()
    if runtime is None:
        return False

    app = runtime.shared_application()
    if app is None:
        logger.debug("NSApplication non disponibile: l'icona nel Dock resta com'e'")
        return False

    wanted = REGULAR if visible else ACCESSORY
    if runtime.activation_policy(app) == wanted:
        return False
    if not runtime.set_activation_policy(app, wanted):
        #macOS rifiuta il cambio in qualche stato
        logger.debug("macOS ha rifiutato il cambio di activation policy a %d", wanted)
        return False
    if visible:
        runtime.activate(app)
    return True