from __future__ import annotations

import ctypes
import logging
import os
import platform
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

#Avvio automatico all'accesso a Windows
logger = logging.getLogger(__name__)

TASK_NAME = "EcoMonitor"
SCRIPT_NAME = "autostart.ps1"

XDG_AUTOSTART_ENTRY = "ecomonitor.desktop"

HIDDEN_START_FLAG = "--background"

#niente finestra di console dietro l'interfaccia grafica
_CREATE_NO_WINDOW = 0x08000000
_SW_HIDE = 0
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_TIMEOUT_MS = 120_000


class AutostartError(RuntimeError):
    pass


SUPPORTED_SYSTEMS = ("Windows", "Linux")


def is_supported() -> bool:
    return platform.system() in SUPPORTED_SYSTEMS and getattr(sys, "frozen", False)


def unsupported_reason() -> str:
    if platform.system() not in SUPPORTED_SYSTEMS:
        return "L'avvio automatico e' disponibile solo su Windows e Linux."
    if not getattr(sys, "frozen", False):
        return "L'avvio automatico e' disponibile solo sull'applicazione installata."
    return ""


def autostart_entry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / XDG_AUTOSTART_ENTRY


def _target_path() -> str:
    return str(Path(sys.executable).resolve())


def script_path() -> Path:
    return Path(sys.executable).resolve().parent / SCRIPT_NAME


def is_elevated() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def needs_elevation() -> bool:
    return platform.system() == "Windows" and not is_elevated()


def is_enabled() -> bool:
    #Interrogare non richiede privilegi
    if platform.system() == "Linux":
        return _linux_is_enabled()
    if platform.system() != "Windows":
        return False
    try:
        result = subprocess.run(
            ["schtasks.exe", "/query", "/tn", TASK_NAME],
            capture_output=True,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Non riesco a interrogare l'avvio automatico: %s", exc)
        return False
    return result.returncode == 0


def starts_hidden() -> bool:
    #Se la voce esistente chiede di partire in secondo piano
    if platform.system() == "Linux":
        return _linux_starts_hidden()
    if platform.system() != "Windows":
        return False
    return HIDDEN_START_FLAG in _windows_task_xml()


def _windows_task_xml() -> str:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/query", "/tn", TASK_NAME, "/xml"],
            capture_output=True,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Non riesco a leggere l'attivita' pianificata: %s", exc)
        return ""
    if result.returncode != 0:
        return ""
    return decode_task_xml(result.stdout)


def decode_task_xml(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def set_enabled(enabled: bool, start_hidden: bool = False) -> None:
    #Solleva AutostartError
    if not is_supported():
        raise AutostartError(unsupported_reason())

    if platform.system() == "Linux":
        _linux_set_enabled(enabled, start_hidden)
        return

    script = script_path()
    if not script.is_file():
        raise AutostartError(
            f"Manca {SCRIPT_NAME} accanto all'applicazione: reinstallare EcoMonitor."
        )

    arguments = ["-Unregister"]
    if enabled:
        arguments = [
            "-Register",
            "-TargetPath",
            str(Path(sys.executable).resolve()),
            "-UserId",
            _current_user_id(),
        ]
        if start_hidden:
            arguments += ["-Arguments", HIDDEN_START_FLAG]

    if is_elevated():
        _run_here(script, arguments)
    else:
        _run_elevated(script, arguments)


def _current_user_id() -> str:
    domain = os.environ.get("USERDOMAIN") or platform.node()
    return domain + "\\" + os.environ.get("USERNAME", "")


def _powershell_arguments(script: Path, arguments: list[str]) -> list[str]:
    return [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
    ]


def _run_here(script: Path, arguments: list[str]) -> None:
    try:
        result = subprocess.run(
            ["powershell.exe", *_powershell_arguments(script, arguments)],
            capture_output=True,
            check=False,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutostartError(f"Comando non eseguito: {exc}") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise AutostartError(message[0] if message else "Il comando e' fallito senza messaggio.")


def _quote(argument: str) -> str:
    return f'"{argument}"' if " " in argument else argument


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    )


def _run_elevated(script: Path, arguments: list[str]) -> None:
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = " ".join(
        _quote(argument) for argument in _powershell_arguments(script, arguments)
    )
    info.nShow = _SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error() or ctypes.GetLastError()
        if code == 1223:
            raise AutostartError("Elevazione rifiutata: l'avvio automatico non e' stato cambiato.")
        raise AutostartError(f"Elevazione non riuscita (codice {code}).")

    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.WaitForSingleObject(info.hProcess, _WAIT_TIMEOUT_MS)
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
    finally:
        kernel32.CloseHandle(info.hProcess)

    if exit_code.value != 0:
        raise AutostartError(
            f"Il comando elevato e' fallito (codice {exit_code.value}). "
            "Controllare Utilita' di pianificazione."
        )


def _linux_entry_text(start_hidden: bool) -> str:
    #X-GNOME-Autostart-enabled
    command = _target_path()
    if start_hidden:
        command += " " + HIDDEN_START_FLAG
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=EcoMonitor\n"
        "Comment=Monitoraggio del consumo energetico, avviato all'accesso\n"
        f"Exec={command}\n"
        #se l'eseguibile non c'e' piu' la voce viene ignorata invece di fallire in
        #silenzio a ogni accesso
        f"TryExec={_target_path()}\n"
        "Icon=ecomonitor\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _linux_exec_value() -> str | None:
    #il valore della riga Exec
    try:
        content = autostart_entry_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("Exec="):
            return stripped.removeprefix("Exec=")
    return None


def _linux_is_enabled() -> bool:
    value = _linux_exec_value()
    if value is None:
        return False
    #l'argomento si toglie dalla coda 
    return value.removesuffix(HIDDEN_START_FLAG).strip() == _target_path()


def _linux_starts_hidden() -> bool:
    value = _linux_exec_value()
    if value is None:
        return False
    return value.rstrip().endswith(HIDDEN_START_FLAG) and _linux_is_enabled()


def _linux_set_enabled(enabled: bool, start_hidden: bool = False) -> None:
    path = autostart_entry_path()
    if not enabled:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise AutostartError(f"Non riesco a rimuovere {path}: {exc}") from exc
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_linux_entry_text(start_hidden), encoding="utf-8")
        path.chmod(0o644)
    except OSError as exc:
        raise AutostartError(f"Non riesco a scrivere {path}: {exc}") from exc
