from collections.abc import Iterable
from dataclasses import dataclass

from ecomonitor.sensors.base import PowerReading, SensorUnavailableError, now_utc

#Backend Windows via il pacchetto HardwareMonitor (PyPI), che include gia' la
#DLL di LibreHardwareMonitor e il wrapper pythonnet
#  Richiede .NET 4.7+.


#Nomi osservati per le GPU integrate: la libreria non distingue integrata e
#dedicata, un'APU AMD e una Radeon sono entrambe GpuAmd. Euristica
_INTEGRATED_GPU_NAME_HINTS = (
    "radeon(tm) graphics",  # APU AMD generiche
    "vega graphics",
    "uhd graphics",
    "iris",
)


def _is_integrated_gpu_name(hardware_name: str) -> bool:
    name = hardware_name.lower()
    return any(hint in name for hint in _INTEGRATED_GPU_NAME_HINTS)


@dataclass
class _ClassifiedPower:
    package_w: float
    dram_w: float | None
    gpu_integrated_w: float | None
    gpu_dedicated_w: float | None
    found_package: bool


def _classify_hardware(hardware_iterable: Iterable, hardware_type, sensor_type) -> _ClassifiedPower:
    #Logica pura di classificazione, separata da read() per essere verificabile
    #con oggetti finti, senza il pacchetto HardwareMonitor ne' hardware reale.
    #Presuppone che hardware.Update() sia gia' stato chiamato
    package_w = 0.0
    dram_w: float | None = None
    gpu_integrated_w: float | None = None
    gpu_dedicated_w: float | None = None
    found_package = False

    for hardware in hardware_iterable:
        if hardware.HardwareType == hardware_type.Cpu:
            for sensor in hardware.Sensors:
                if sensor.SensorType != sensor_type.Power or not sensor.Value:
                    continue  # not sensor.Value scarta sia None che 0.0
                if "Package" in sensor.Name:
                    #il Package e' la somma di tutti i core: e' l'aggregato che
                    #corrisponde al dominio "package" di RAPL. I singoli
                    #"CPU Core #N" sarebbero un doppio conteggio
                    package_w += float(sensor.Value)
                    found_package = True
                elif "Memory" in sensor.Name:
                    #su Intel il dominio DRAM e' esposto come sensore della CPU,
                    #non dell'hardware Memory. Assente su AMD
                    dram_w = (dram_w or 0.0) + float(sensor.Value)

        elif hardware.HardwareType in (
            hardware_type.GpuNvidia,
            hardware_type.GpuAmd,
            hardware_type.GpuIntel,
        ):
            gpu_power = sum(
                float(sensor.Value)
                for sensor in hardware.Sensors
                if sensor.SensorType == sensor_type.Power and sensor.Value is not None
            )
            if gpu_power == 0.0:
                continue

            #NVIDIA non produce grafica integrata in CPU: e' sempre dedicata
            is_integrated = hardware.HardwareType != hardware_type.GpuNvidia and (
                _is_integrated_gpu_name(hardware.Name)
            )
            if is_integrated:
                gpu_integrated_w = (gpu_integrated_w or 0.0) + gpu_power
            else:
                gpu_dedicated_w = (gpu_dedicated_w or 0.0) + gpu_power

        elif hardware.HardwareType == hardware_type.Memory:
            for sensor in hardware.Sensors:
                if sensor.SensorType == sensor_type.Power and sensor.Value is not None:
                    dram_w = (dram_w or 0.0) + float(sensor.Value)

    return _ClassifiedPower(package_w, dram_w, gpu_integrated_w, gpu_dedicated_w, found_package)


class WindowsLhmBackend:
    def __init__(self) -> None:
        try:
            from HardwareMonitor.Hardware import HardwareType, SensorType
            from HardwareMonitor.Util import OpenComputer
        except ImportError as exc:
            raise SensorUnavailableError(
                "Pacchetto HardwareMonitor non installato — "
                "eseguire: pip install -e \".[windows]\""
            ) from exc

        self._HardwareType = HardwareType
        self._SensorType = SensorType

        try:
            self._computer = OpenComputer(cpu=True, gpu=True, memory=True)
        except Exception as exc:  # pythonnet propaga eccezioni .NET generiche
            raise SensorUnavailableError(
                "Impossibile aprire LibreHardwareMonitor — servono privilegi di "
                "amministratore (riavviare il processo come admin)"
            ) from exc

        if not self._has_cpu_package_sensor():
            self._computer.Close()
            raise SensorUnavailableError(
                "Nessun sensore 'CPU Package' trovato — CPU non supportata o "
                "privilegi insufficienti per leggere gli MSR"
            )

    def _has_cpu_package_sensor(self) -> bool:
        #True solo se il Package esiste E riporta un valore non nullo: un 0.0
        #indica quasi sempre permessi insufficienti, non consumo reale
        try:
            for hardware in self._computer.Hardware:
                if hardware.HardwareType == self._HardwareType.Cpu:
                    hardware.Update()
                    for sensor in hardware.Sensors:
                        if (
                            sensor.SensorType == self._SensorType.Power
                            and "Package" in sensor.Name
                            and sensor.Value
                        ):
                            return True
            return False
        except Exception:
            return False

    def read(self) -> PowerReading:
        for hardware in self._computer.Hardware:
            hardware.Update()

        result = _classify_hardware(self._computer.Hardware, self._HardwareType, self._SensorType)

        if not result.found_package:
            raise SensorUnavailableError(
                "Il sensore 'CPU Package' non ha restituito un valore in questo tick "
                "(hardware non supportato o permessi persi a runtime)"
            )

        return PowerReading(
            package_w=result.package_w,
            dram_w=result.dram_w,
            gpu_integrated_w=result.gpu_integrated_w,
            gpu_dedicated_w=result.gpu_dedicated_w,
            timestamp=now_utc(),
            is_estimated=False,
        )

    def close(self) -> None:
        self._computer.Close()
