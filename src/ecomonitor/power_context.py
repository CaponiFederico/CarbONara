from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

import psutil

#Stato collegato/scollegato e percentuale di batteria. Serve SOLO come contesto
#per decidere quale logica di attribuzione applicare (rete adesso o pool
#batteria): la misura di energia arriva sempre e solo dai sensori hardware

POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")


@dataclass(frozen=True)
class PowerSourceState:
    is_plugged_in: bool
    battery_percent: float | None  # None su desktop senza batteria


def _read_attribute(entry: Path, name: str) -> str | None:
    try:
        return (entry / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _linux_system_battery() -> Path | None:
    #scope=Device e' la batteria di una periferica
    try:
        entries = sorted(POWER_SUPPLY_ROOT.iterdir())
    except OSError:
        return None
    for entry in entries:
        if _read_attribute(entry, "type") != "Battery":
            continue
        if _read_attribute(entry, "scope") == "Device":
            continue
        return entry
    return None


def _linux_mains_online() -> bool | None:
    try:
        entries = sorted(POWER_SUPPLY_ROOT.iterdir())
    except OSError:
        return None
    for entry in entries:
        if _read_attribute(entry, "type") != "Mains":
            continue
        online = _read_attribute(entry, "online")
        if online is not None:
            return online == "1"
    return None


def _linux_is_plugged_in(battery: Path) -> bool:
    status = (_read_attribute(battery, "status") or "").lower()
    if status == "discharging":
        return False
    if status in ("charging", "full"):
        return True
    #stato incerto: se la rete e' online, consideriamo il portatile collegato
    mains = _linux_mains_online()
    return True if mains is None else mains


def read_power_source_state() -> PowerSourceState:
    if platform.system() == "Linux":
        battery = _linux_system_battery()
        if battery is None:
            return PowerSourceState(is_plugged_in=True, battery_percent=None)  # desktop
        capacity = _read_attribute(battery, "capacity")
        if capacity is not None:
            try:
                percent = float(capacity)
            except ValueError:
                percent = -1.0
            if percent >= 0.0:
                return PowerSourceState(
                    is_plugged_in=_linux_is_plugged_in(battery),
                    battery_percent=percent,
                )
        #sysfs incompleto: si ricade su psutil invece di inventare un valore

    battery_state = psutil.sensors_battery()
    if battery_state is None:
        return PowerSourceState(is_plugged_in=True, battery_percent=None)  # desktop
    return PowerSourceState(
        is_plugged_in=battery_state.power_plugged,
        battery_percent=battery_state.percent,
    )
