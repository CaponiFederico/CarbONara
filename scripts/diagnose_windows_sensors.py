# Elenca tutto l'hardware e tutti i sensori (non solo quelli di tipo Power)
# che LibreHardwareMonitor vede su questa macchina. Serve a distinguere
# "sensore assente o non supportato dall'hardware" da "bug nel nostro filtro"
# (vedi ecomonitor/sensors/windows_lhm.py).
#
# Da eseguire in un terminale da amministratore, con pip install -e ".[windows]":
#     python scripts/diagnose_windows_sensors.py
#
# Punti emersi provando su macchine reali:
# - su portatili con grafica ibrida la GPU dedicata spesso non ha un sensore di
#   potenza attivo quando e' spenta per risparmio energetico: non e' un bug
# - l'hardware "Memory" espone quasi sempre solo utilizzo e clock, mai potenza:
#   per questo di CPU e Motherboard stampiamo TUTTI i sensori, per cercare un
#   eventuale rail pertinente con un nome diverso da "DRAM"

from HardwareMonitor.Hardware import HardwareType
from HardwareMonitor.Util import OpenComputer


def main() -> None:
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
