# Elenca tutto l'hardware e tutti i sensori (non solo quelli di tipo Power)
# che LibreHardwareMonitor vede su questa macchina
#
# Da eseguire in un terminale da amministratore con pip install -e ".[windows]":
#     python scripts/diagnose_windows_sensors.py
import ctypes

from HardwareMonitor.Hardware import HardwareType
from HardwareMonitor.Util import OpenComputer

from ecomonitor.sensors.windows_lhm import pawnio_installed


def print_prerequisites() -> None:
    elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    print(f"processo elevato: {'si' if elevated else 'NO - CPU Package restera vuoto'}")
    pawnio = pawnio_installed()
    if pawnio is None:
        print("driver PawnIO: non determinabile")
    else:
        print(f"driver PawnIO: {'installato' if pawnio else 'ASSENTE - CPU Package restera vuoto'}")
    print()


def main() -> None:
    print_prerequisites()
    computer = OpenComputer(cpu=True, gpu=True, memory=True, motherboard=True)
    try:
        for hardware in computer.Hardware:
            hardware.Update()
            print(f"=== {hardware.Name} ({hardware.HardwareType}) ===")

            if hardware.HardwareType in (HardwareType.Cpu, HardwareType.Motherboard):
                for sensor in hardware.Sensors:
                    print(f"  [{sensor.SensorType}] {sensor.Name}: {sensor.Value}")
                for sub in hardware.SubHardware:
                    sub.Update()
                    print(f"  --- sub-hardware: {sub.Name} ---")
                    for sensor in sub.Sensors:
                        print(f"    [{sensor.SensorType}] {sensor.Name}: {sensor.Value}")
            else:
                from HardwareMonitor.Hardware import SensorType

                power_sensors = [s for s in hardware.Sensors if s.SensorType == SensorType.Power]
                if not power_sensors:
                    print("  (nessun sensore di tipo Power su questo hardware)")
                for sensor in power_sensors:
                    print(f"  {sensor.Name}: {sensor.Value}")
    finally:
        computer.Close()


if __name__ == "__main__":
    main()
