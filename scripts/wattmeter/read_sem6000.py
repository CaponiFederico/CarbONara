# Registrazione della potenza letta dalla presa Voltcraft SEM6000 via Bluetooth LE
#
# Protocollo: ricostruito da github.com/Heckie75/voltcraft-sem-6000
#
# A differenza della Sonoff la presa si interroga: una richiesta, una risposta,
# a cadenza fissa anche a carico costante
#
#     python scripts/wattmeter/read_sem6000.py --list
#     python scripts/wattmeter/read_sem6000.py --address <indirizzo> --output measurements/plug.csv
#
# Dipendenze:  pip install -e ".[validation]"

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"

LOGIN_COMMAND = 0x17
MEASURE_COMMAND = 0x04

#Secondi di attesa di una risposta prima di considerare persa la connessione
REPLY_TIMEOUT = 5.0
#Pausa fra due tentativi di riconnessione
RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0

#Stesso header di read_plug.py
HEADER = [
    "timestamp_utc",
    "id",
    "power_w",
    "voltage_v",
    "current_a",
    "on",
    "params_json",
]


def checksum(payload: bytes) -> int:
    return (sum(payload) + 1) & 0xFF


def frame(payload: bytes) -> bytes:
    return bytes([0x0F, len(payload) + 1]) + payload + bytes([checksum(payload), 0xFF, 0xFF])


def login_frame(pin: str) -> bytes:
    digits = bytes(int(c) for c in pin)
    return frame(bytes([LOGIN_COMMAND, 0x00, 0x00]) + digits + bytes(4))


def measure_frame() -> bytes:
    return frame(bytes([MEASURE_COMMAND, 0x00, 0x00, 0x00]))


def reply_is_valid(data: bytes, command: int) -> bool:
    body = data[:-2] if data.endswith(b"\xff\xff") else data
    if len(body) < 4 or body[0] != 0x0F or body[2] != command:
        return False
    return checksum(body[2:-1]) == body[-1]


def login_succeeded(data: bytes) -> bool:
    return reply_is_valid(data, LOGIN_COMMAND) and len(data) > 5 and data[5] == 0x00


def parse_measure(data: bytes) -> dict | None:
    #Campi documentati:
    #  byte 4      stato del relè
    #  byte 5..7   potenza in mW
    #  byte 8      tensione in V 
    #  byte 9..10  corrente in mA
    #  byte 11     frequenza in Hz 
    if not reply_is_valid(data, MEASURE_COMMAND) or len(data) < 13:
        return None
    return {
        "on": data[4],
        "power_w": int.from_bytes(data[5:8], "big") / 1000.0,
        "voltage_v": float(data[8]),
        "current_a": int.from_bytes(data[9:11], "big") / 1000.0,
        "frequency_hz": data[11],
    }


def csv_row(timestamp: datetime, device_id: str, data: bytes, values: dict) -> list:
    return [
        timestamp.isoformat(),
        device_id,
        values["power_w"],
        values["voltage_v"],
        values["current_a"],
        values["on"],
        json.dumps(
            {"frequency_hz": values["frequency_hz"], "raw": data.hex()},
            separators=(",", ":"),
            sort_keys=True,
        ),
    ]


def _bleak():
    try:
        import bleak
    except ImportError:
        print("manca bleak: pip install -e \".[validation]\"", file=sys.stderr)
        raise SystemExit(2) from None
    return bleak


async def discover(seconds: float) -> int:
    bleak = _bleak()
    print(f"Ricerca di prese SEM6000 per {seconds:.0f} s...")
    found = await bleak.BleakScanner.discover(timeout=seconds, return_adv=True)

    plugs = [
        (address, adv)
        for address, (_, adv) in found.items()
        if SERVICE_UUID in (adv.service_uuids or []) or (adv.local_name or "") == "Voltcraft"
    ]
    if not plugs:
        print(
            "Nessuna presa trovata. Da controllare:\n"
            "  - la presa e' alimentata e vicina?\n"
            "  - l'app Voltcraft sul telefono e' chiusa? Se e' collegata, la presa\n"
            "    smette di annunciarsi\n"
            "  - il Bluetooth di questa macchina e' acceso?"
        )
        return 1

    print("\nPrese trovate:")
    for address, adv in sorted(plugs, key=lambda item: -item[1].rssi):
        print(f"  address={address}  nome={adv.local_name!r}  rssi={adv.rssi} dBm")
    print("\nUsa l'indirizzo con --address.")
    return 0


class Session:
    def __init__(self, address: str, pin: str, interval: float, write, verbose: bool):
        self.address = address
        self.pin = pin
        self.interval = interval
        self.write = write
        self.verbose = verbose
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.readings = 0
        self.invalid = 0
        self.missed = 0

    def _on_notify(self, _, data: bytearray) -> None:
        self.replies.put_nowait(bytes(data))

    async def _reply(self, command: int) -> bytes | None:
        deadline = time.monotonic() + REPLY_TIMEOUT
        while (left := deadline - time.monotonic()) > 0:
            try:
                data = await asyncio.wait_for(self.replies.get(), left)
            except TimeoutError:
                return None
            if len(data) > 2 and data[2] == command:
                return data
        return None

    async def run(self, end: float | None) -> None:
        bleak = _bleak()
        async with bleak.BleakClient(self.address, timeout=20.0) as client:
            await client.start_notify(NOTIFY_UUID, self._on_notify)

            await client.write_gatt_char(WRITE_UUID, login_frame(self.pin), response=False)
            reply = await self._reply(LOGIN_COMMAND)
            if reply is None:
                raise ConnectionError("nessuna risposta all'accesso")
            if not login_succeeded(reply):
                raise PermissionError(f"PIN rifiutato dalla presa (risposta {reply.hex(' ')})")

            print(f"Collegato a {self.address}, accesso riuscito.")
            next_at = time.monotonic()
            while end is None or time.monotonic() < end:
                await client.write_gatt_char(WRITE_UUID, measure_frame(), response=False)
                data = await self._reply(MEASURE_COMMAND)
                moment = datetime.now(UTC)
                if data is None:
                    self.missed += 1
                    raise ConnectionError("la presa ha smesso di rispondere")

                values = parse_measure(data)
                if values is None:
                    #una risposta corrotta non ferma la sessione
                    self.invalid += 1
                    if self.verbose or self.invalid == 1:
                        print(f"  risposta non valida: {data.hex(' ')}", file=sys.stderr)
                else:
                    self.write(csv_row(moment, self.address, data, values))
                    self.readings += 1
                    if self.verbose:
                        print(f"  {moment.isoformat()}  {values['power_w']:.3f} W  "
                              f"{values['voltage_v']:.0f} V  {values['current_a']:.3f} A")

                next_at += self.interval
                delay = next_at - time.monotonic()
                if delay < 0:
                    next_at = time.monotonic()
                    delay = 0
                await asyncio.sleep(delay)


async def record(address: str, pin: str, interval: float, output: Path,
                 minutes: float | None, verbose: bool) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    is_new = not output.exists() or output.stat().st_size == 0

    start = time.monotonic()
    end = start + minutes * 60.0 if minutes else None
    disconnections = 0

    with output.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(HEADER)
            f.flush()

        def write(row):
            #flush a ogni riga
            writer.writerow(row)
            f.flush()

        session = Session(address, pin, interval, write, verbose)
        print(f"Registrazione su {output}. Ctrl+C per fermare.")

        backoff = RECONNECT_MIN
        try:
            while end is None or time.monotonic() < end:
                before = session.readings
                try:
                    await session.run(end)
                except PermissionError as error:
                    print(error, file=sys.stderr)
                    return 2
                except Exception as error:
                    #una connessione persa non chiude la sessione
                    disconnections += 1
                    if session.readings > before:
                        backoff = RECONNECT_MIN
                    print(f"  connessione persa ({error}), riprovo fra {backoff:.0f} s",
                          file=sys.stderr)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)
        finally:
            duration = time.monotonic() - start
            print(
                f"\n{session.readings} letture in {duration / 60.0:.1f} min, "
                f"{session.invalid} risposte non valide, "
                f"{disconnections} disconnessioni -> {output}"
            )
    return 0 if session.readings else 1


def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita
    parser = argparse.ArgumentParser(
        description="Registra in CSV la potenza letta via Bluetooth da una presa Voltcraft SEM6000"
    )
    parser.add_argument("--address", default=None, help="indirizzo Bluetooth della presa")
    parser.add_argument("--pin", default="0000", help="PIN della presa, predefinito 0000")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="secondi fra due misure, predefinito 1")
    parser.add_argument(
        "--output",
        default=None,
        help="file CSV, in append; predefinito measurements/sem6000_<date>.csv",
    )
    parser.add_argument("--minutes", type=float, default=None, help="durata, altrimenti Ctrl+C")
    parser.add_argument("--list", action="store_true",
                        help="cerca le prese nelle vicinanze ed esce")
    parser.add_argument("--discovery-seconds", type=float, default=10.0)
    parser.add_argument("--verbose", action="store_true", help="stampa ogni lettura")
    args = parser.parse_args(argv)

    if args.list:
        return asyncio.run(discover(args.discovery_seconds))

    if not args.address:
        parser.error("serve --address, oppure --list per trovarlo")
    if len(args.pin) != 4 or not args.pin.isdigit():
        parser.error("--pin deve essere di quattro cifre")
    if args.interval <= 0:
        parser.error("--interval deve essere positivo")
    if args.minutes is not None and args.minutes <= 0:
        parser.error("--minutes deve essere positivo")

    if args.output:
        output = Path(args.output)
    else:
        output = Path("measurements") / f"sem6000_{datetime.now(UTC):%Y%m%d_%H%M%S}.csv"

    try:
        return asyncio.run(record(args.address, args.pin, args.interval, output,
                                  args.minutes, args.verbose))
    except KeyboardInterrupt:
        print("\ninterrotto")
        return 0


if __name__ == "__main__":
    sys.exit(main())
