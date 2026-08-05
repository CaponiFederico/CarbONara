from dataclasses import dataclass

import psutil

#Stato collegato/scollegato e percentuale di batteria. Serve SOLO come contesto
#per decidere quale logica di attribuzione applicare (rete adesso o pool
#batteria): la misura di energia arriva sempre e solo dai sensori hardware


@dataclass(frozen=True)
class PowerSourceState:
    is_plugged_in: bool
    battery_percent: float | None  # None su desktop senza batteria


def read_power_source_state() -> PowerSourceState:
    battery = psutil.sensors_battery()
    if battery is None:
        return PowerSourceState(is_plugged_in=True, battery_percent=None)  # desktop
    return PowerSourceState(is_plugged_in=battery.power_plugged, battery_percent=battery.percent)
