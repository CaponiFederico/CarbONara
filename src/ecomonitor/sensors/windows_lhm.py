import re
from collections.abc import Iterable
from dataclasses import dataclass

from ecomonitor.sensors.base import (
    PowerReading,
    SensorUnavailableError,
    now_utc,
    sample_cpu_percent,
)
from ecomonitor.sensors.calibration import PowerCalibration
from ecomonitor.sensors.fallback_model import DEFAULT_TDP_W

#Backend Windows via il pacchetto HardwareMonitor (PyPI), che include gia' la
#DLL di LibreHardwareMonitor e il wrapper pythonnet
#  Richiede .NET 4.7+.


#Nomi osservati per le GPU integrate: la libreria non distingue integrata e
#dedicata, un'APU AMD e una Radeon sono entrambe GpuAmd. Euristica
_INTEGRATED_GPU_NAME_HINTS = (
    "vega graphics",
    "uhd graphics",
    "iris",
)

#Le regole sui nomi Radeon 
_INTEGRATED_GPU_NAME_PATTERNS = (
    #"AMD Radeon(TM) Graphics"
    re.compile(r"radeon(\(tm\))?\s+graphics"),
    re.compile(r"radeon\s+\d{3}m\b"),  # modello a tre cifre + M subito dopo "Radeon"
    re.compile(r"vega\s+\d+\s+graphics"),  # "Vega 8 Graphics", "Vega 11 Graphics"
)


def _is_integrated_gpu_name(hardware_name: str) -> bool:
    name = hardware_name.lower()
    if any(hint in name for hint in _INTEGRATED_GPU_NAME_HINTS):
        return True
    return any(pattern.search(name) for pattern in _INTEGRATED_GPU_NAME_PATTERNS)


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
    #Quota di gpu_integrated_w che il sensore conta gia' dentro di se'
    gpu_integrated_inside_package_w = 0.0

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
                if hardware.HardwareType == hardware_type.GpuAmd:
                    continue

                gpu_integrated_w = (gpu_integrated_w or 0.0) + gpu_power
                if hardware.HardwareType == hardware_type.GpuIntel:
                    gpu_integrated_inside_package_w += gpu_power
            else:
                gpu_dedicated_w = (gpu_dedicated_w or 0.0) + gpu_power

        elif hardware.HardwareType == hardware_type.Memory:
            for sensor in hardware.Sensors:
                if sensor.SensorType == sensor_type.Power and sensor.Value is not None:
                    dram_w = (dram_w or 0.0) + float(sensor.Value)

    #Sui client Intel la grafica integrata sta sullo stesso package della CPU 
    if gpu_integrated_inside_package_w:
        package_w = max(0.0, package_w - gpu_integrated_inside_package_w)

    return _ClassifiedPower(package_w, dram_w, gpu_integrated_w, gpu_dedicated_w, found_package)


def _open_lhm():
    #Apertura condivisa fra il backend completo e quello parziale
    try:
        from HardwareMonitor.Hardware import HardwareType, SensorType
        from HardwareMonitor.Util import OpenComputer
    except ImportError as exc:
        raise SensorUnavailableError(
            "Pacchetto HardwareMonitor non installato — "
            "eseguire: pip install -e \".[windows]\""
        ) from exc

    try:
        computer = OpenComputer(cpu=True, gpu=True, memory=True)
    except Exception as exc:  # pythonnet propaga eccezioni .NET generiche
        raise SensorUnavailableError(
            "Impossibile aprire LibreHardwareMonitor"
        ) from exc

    return computer, HardwareType, SensorType


class WindowsLhmBackend:
    def __init__(self) -> None:
        self._computer, self._HardwareType, self._SensorType = _open_lhm()

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
            #registrato accanto alla potenza misurata: e' l'ascissa su cui si
            #calibra il ripiego per quando questi sensori non saranno leggibili
            cpu_percent=sample_cpu_percent(),
        )

    def close(self) -> None:
        self._computer.Close()


class WindowsPartialLhmBackend:
    #GPU dedicata MISURATA, CPU stimata

    def __init__(
        self,
        tdp_w: float = DEFAULT_TDP_W,
        cpu_calibration: PowerCalibration | None = None,
    ) -> None:
        self._computer, self._HardwareType, self._SensorType = _open_lhm()
        self.tdp_w = tdp_w
        #Se c'e', il termine CPU viene dallo storico di questa macchina invece
        #che da un TDP generico
        self.cpu_calibration = cpu_calibration

        if not self._dedicated_gpu_power():
            self._computer.Close()
            raise SensorUnavailableError(
                "Nessuna GPU dedicata con potenza leggibile: senza quella questo "
                "backend non aggiunge niente alla stima generica"
            )

        sample_cpu_percent()  # la prima chiamata vale sempre 0.0

    def _dedicated_gpu_power(self) -> float | None:
        try:
            for hardware in self._computer.Hardware:
                hardware.Update()
            result = _classify_hardware(
                self._computer.Hardware, self._HardwareType, self._SensorType
            )
        except Exception:
            return None
        return result.gpu_dedicated_w

    def read(self) -> PowerReading:
        for hardware in self._computer.Hardware:
            hardware.Update()

        result = _classify_hardware(self._computer.Hardware, self._HardwareType, self._SensorType)
        cpu_percent = sample_cpu_percent() or 0.0

        if self.cpu_calibration is not None:
            package_w = self.cpu_calibration.predict(cpu_percent)
        else:
            package_w = self.tdp_w * (cpu_percent / 100.0)

        return PowerReading(
            #stimato: e' il dominio che senza privilegi non si legge
            package_w=package_w,
            dram_w=result.dram_w,
            gpu_integrated_w=None,
            gpu_dedicated_w=result.gpu_dedicated_w,
            timestamp=now_utc(),
            is_estimated=True,
            cpu_percent=cpu_percent,
        )

    def close(self) -> None:
        self._computer.Close()
