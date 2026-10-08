#   Specifiche PyInstaller condivisa fra Windows, Linux e macOS
#
#   pyinstaller --clean --noconfirm packaging/ecomonitor.spec
#
#   EcoMonitor/            applicazione grafica 
#   ecomonitor-cli(.exe)   eseguibile di collaudo, non spedito nel pacchetto

import os
import re
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

PROJECT_ROOT = Path(SPECPATH).parent
SRC_PATH = str(PROJECT_ROOT / "src")

VERSION = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]
_init_version = re.search(
    r'^__version__ = "([^"]+)"',
    (PROJECT_ROOT / "src" / "ecomonitor" / "__init__.py").read_text(encoding="utf-8"),
    re.MULTILINE,
)
if _init_version is None or _init_version.group(1) != VERSION:
    raise SystemExit(
        f"Versioni diverse: pyproject.toml dice {VERSION}, src/ecomonitor/__init__.py "
        f"dice {_init_version.group(1) if _init_version else 'niente'}. Aggiornarle entrambe."
    )

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"

#lo stesso di autostart.LAUNCH_AGENT_LABEL
BUNDLE_IDENTIFIER = "io.github.caponifederico.ecomonitor"

#Su Windows i sensori veri richiedono un processo elevato. 
#build con permessi alti obbligati: ECOMONITOR_UAC_ADMIN=1
REQUEST_UAC_ADMIN = os.environ.get("ECOMONITOR_UAC_ADMIN") == "1"

ICON_PATH = PROJECT_ROOT / "packaging" / "ecomonitor.ico"
if IS_WINDOWS and not ICON_PATH.exists():
    raise SystemExit("Manca packaging/ecomonitor.ico: eseguire prima python packaging/make_icon.py")
ICNS_PATH = PROJECT_ROOT / "packaging" / "ecomonitor.icns"
if IS_MACOS and not ICNS_PATH.exists():
    raise SystemExit("Manca packaging/ecomonitor.icns: eseguire prima python packaging/make_icon.py")

if IS_WINDOWS:
    EXE_ICON = str(ICON_PATH)
elif IS_MACOS:
    EXE_ICON = str(ICNS_PATH)
else:
    EXE_ICON = None

#Moduli caricati solo a runtime in base al sistema
sensor_hidden_imports = ["ecomonitor.sensors.fallback_model"]
if IS_WINDOWS:
    sensor_hidden_imports += ["ecomonitor.sensors.windows_lhm", "clr"]
elif IS_LINUX:
    sensor_hidden_imports += ["ecomonitor.sensors.linux_rapl", "ecomonitor.sensors.linux_gpu"]
else:
    sensor_hidden_imports += ["ecomonitor.sensors.macos_powermetrics"]

#pythonnet carica LibreHardwareMonitor da DLL .NET 
sensor_datas: list = []
sensor_binaries: list = []
if IS_WINDOWS:
    for package_name in ("HardwareMonitor", "clr_loader", "pythonnet"):
        package_datas, package_binaries, package_hidden = collect_all(package_name)
        sensor_datas += package_datas
        sensor_binaries += package_binaries
        sensor_hidden_imports += package_hidden

#Niente di tutto questo viene importato
COMMON_EXCLUDES = ["tkinter", "unittest", "pydoc_data", "pytest", "_pytest", "numpy"]
QT_EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.Qt3DCore",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtPdf",
    "PySide6.QtSql",
    "PySide6.QtTest",
]

#Notifiche native di Windows
gui_hidden_imports = list(sensor_hidden_imports)
if IS_WINDOWS:
    gui_hidden_imports += [
        "ecomonitor.gui.windows_toast",
        "winrt.windows.data.xml.dom",
        "winrt.windows.ui.notifications",
    ]
#Notifiche native di macOS (PyObjC)
if IS_MACOS:
    gui_hidden_imports += [
        "ecomonitor.gui.macos_notifier",
        "objc",
        "CoreFoundation",
        "Foundation",
        "UserNotifications",
    ]

gui_analysis = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "packaging" / "entry_gui.py")],
    pathex=[SRC_PATH],
    binaries=sensor_binaries,
    datas=sensor_datas,
    hiddenimports=gui_hidden_imports,
    excludes=COMMON_EXCLUDES + QT_EXCLUDES,
    noarchive=False,
)
gui_pyz = PYZ(gui_analysis.pure)  # noqa: F821
gui_exe = EXE(  # noqa: F821
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="EcoMonitor",
    console=False,  # applicazione grafica
    icon=EXE_ICON,
    uac_admin=REQUEST_UAC_ADMIN,
)
gui_collect_parts = [gui_exe, gui_analysis.binaries, gui_analysis.datas]

SHOW_WINDOW_NAME = "EcoMonitorShow"
if IS_WINDOWS:
    show_analysis = Analysis(  # noqa: F821
        [str(PROJECT_ROOT / "packaging" / "entry_show.py")],
        pathex=[SRC_PATH],
        excludes=COMMON_EXCLUDES + ["PySide6", "shiboken6"],
        noarchive=False,
    )
    show_pyz = PYZ(show_analysis.pure)  # noqa: F821
    show_exe = EXE(  # noqa: F821
        show_pyz,
        show_analysis.scripts,
        [],
        exclude_binaries=True,
        name=SHOW_WINDOW_NAME,
        console=False,  # nessuna console che lampeggia al clic
        icon=EXE_ICON,
        uac_admin=False,
    )
    gui_collect_parts += [show_exe, show_analysis.binaries, show_analysis.datas]

gui_collect = COLLECT(  # noqa: F821
    *gui_collect_parts,
    name="EcoMonitor",
)

#Su macOS l'applicazione e' la cartella EcoMonitor.app accanto a EcoMonitor/
if IS_MACOS:
    BUNDLE(  # noqa: F821
        gui_collect,
        name="EcoMonitor.app",
        icon=str(ICNS_PATH),
        bundle_identifier=BUNDLE_IDENTIFIER,
        info_plist={
            "CFBundleName": "EcoMonitor",
            "CFBundleDisplayName": "EcoMonitor",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )

#La versione a riga di comando non tocca Qt
cli_analysis = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "packaging" / "entry_cli.py")],
    pathex=[SRC_PATH],
    binaries=sensor_binaries,
    datas=sensor_datas,
    hiddenimports=sensor_hidden_imports,
    excludes=COMMON_EXCLUDES + ["PySide6", "shiboken6"],
    noarchive=False,
)
cli_pyz = PYZ(cli_analysis.pure)  # noqa: F821
EXE(  # noqa: F821
    cli_pyz,
    cli_analysis.scripts,
    cli_analysis.binaries,
    cli_analysis.datas,
    [],
    name="ecomonitor-cli",
    console=True,
    icon=EXE_ICON,
)
