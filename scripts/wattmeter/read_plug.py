# Registrazione della potenza letta dalla presa Sonoff in LAN
#
# Protocollo: eWeLink 
#
# Non c'e' polling: gli aggiornamenti li manda il dispositivo quando cambia
# qualcosa
#
#     python scripts/wattmeter/read_plug.py --list
#     python scripts/wattmeter/read_plug.py --key <devicekey> --output measurements/plug.csv
#
# Dipendenze:  pip install -e ".[validation]"

import argparse
import base64
import csv
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SERVICE = "_ewelink._tcp.local."

#Secondi senza vedere nessun dispositivo prima di dire che qualcosa non va
WAIT_BEFORE_DIAGNOSIS = 45.0

#Chiavi dei parametri decifrati 
FIELDS = {
    "power_w": ("power", "actPow", "power_01"),
    "voltage_v": ("voltage", "voltage_01"),
    "current_a": ("current", "current_01"),
}

#Il S61 pubblica potenza, tensione e corrente come interi in centesimi
MAINS_MIN_V = 85.0
MAINS_MAX_V = 260.0
HUNDREDTHS = 0.01

HEADER = [
    "timestamp_utc",
    "id",
    "power_w",
    "voltage_v",
    "current_a",
    "on",
    "params_json",
]


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strip_padding(data: bytes) -> bytes:
    #PKCS#7: l'ultimo byte dice quanti byte di riempimento togliere
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def decrypt(properties: dict[str, str], devicekey: str) -> dict | None:
    #Ritorna i parametri del dispositivo, None se il record non ne contiene

    chunks = "".join(properties.get(f"data{i}", "") for i in range(1, 5))
    if not chunks:
        return None
    raw = base64.b64decode(chunks)

    if properties.get("encrypt", "").lower() not in ("true", "1"):
        return json.loads(raw)

    if not devicekey:
        raise ValueError(
            "il record e' cifrato ma non e' stata fornita la devicekey (--key)"
        )

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = base64.b64decode(properties["iv"])
    #MD5  e' la derivazione di chiave del protocollo eWeLink
    key = hashlib.md5(devicekey.encode(), usedforsecurity=False).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(raw) + decryptor.finalize()
    return json.loads(_strip_padding(plain))


def scale_for(raw_voltage: float | None) -> float | None:
    #Il fattore da applicare alle tre grandezze
    if raw_voltage is None:
        return 1.0
    if MAINS_MIN_V <= raw_voltage <= MAINS_MAX_V:
        return 1.0
    if MAINS_MIN_V <= raw_voltage * HUNDREDTHS <= MAINS_MAX_V:
        return HUNDREDTHS
    return None


def csv_row(timestamp: datetime, device_id: str, params: dict) -> list:

    values = {}
    for column, candidates in FIELDS.items():
        values[column] = next(
            (_number(params[k]) for k in candidates if k in params), None
        )

    scale = scale_for(values["voltage_v"])
    for column in FIELDS:
        if values[column] is not None:
            values[column] = None if scale is None else values[column] * scale

    switch = params.get("switch")
    if switch is None:
        outlets = params.get("switches") or []
        switch = outlets[0].get("switch") if outlets else None

    return [
        timestamp.isoformat(),
        device_id,
        values["power_w"],
        values["voltage_v"],
        values["current_a"],
        1 if switch == "on" else 0 if switch == "off" else None,
        json.dumps(params, separators=(",", ":"), sort_keys=True),
    ]


class Listener:
    #Riceve gli annunci mDNS e li trasforma in righe CSV decifrando i parametri con la devicekey

    def __init__(self, devicekey: str, only_id: str | None, write, verbose: bool,
                 discovery_only: bool = False, keys: dict[str, str] | None = None):
        self.devicekey = devicekey
        #chiavi per id, da device_key.py
        self.keys = keys or {}
        self.only_id = only_id
        self.write = write
        self.verbose = verbose
        self.discovery_only = discovery_only
        self.seen: dict[str, str] = {}
        self.readings = 0
        self.errors = 0
        self.devices: dict[str, str] = {}

    def key_for(self, device_id: str) -> str:
        return self.keys.get(device_id) or self.devicekey

    def add_service(self, zc, service_type, name):
        self._handle(zc, service_type, name)

    def update_service(self, zc, service_type, name):
        self._handle(zc, service_type, name)

    def remove_service(self, zc, service_type, name):
        pass

    def _handle(self, zc, service_type, name):
        info = zc.get_service_info(service_type, name, timeout=3000)
        if info is None:
            return

        properties = {}
        for key, value in (info.properties or {}).items():
            try:
                k = key.decode() if isinstance(key, bytes) else str(key)
                v = value.decode() if isinstance(value, bytes) else str(value)
            except UnicodeDecodeError:
                continue
            properties[k] = v

        device_id = properties.get("id", name.split(".")[0])
        addresses = info.parsed_addresses()
        self.devices[device_id] = addresses[0] if addresses else "?"

        if self.discovery_only or (self.only_id and device_id != self.only_id):
            return

        fingerprint = "".join(properties.get(f"data{i}", "") for i in range(1, 5))
        if not fingerprint or self.seen.get(device_id) == fingerprint:
            return
        self.seen[device_id] = fingerprint

        try:
            params = decrypt(properties, self.key_for(device_id))
        except Exception as error:
            #la sessione non va persa per un record
            self.errors += 1
            if self.verbose or self.errors == 1:
                print(f"  lettura non decifrabile da {device_id}: {error}", file=sys.stderr)
            return

        if not params:
            return

        row = csv_row(datetime.now(UTC), device_id, params)
        self.write(row)
        self.readings += 1
        if self.verbose:
            print(f"  {row[0]}  {device_id}  {row[2]} W  {row[3]} V  {row[4]} A")


def _browser(listener):
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        print(
            "manca zeroconf: pip install -e \".[validation]\"",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    zc = Zeroconf()
    ServiceBrowser(zc, SERVICE, listener)
    return zc


def _diagnosis() -> str:
    return (
        "Nessun dispositivo eWeLink annunciato in rete. Da controllare:\n"
        "  - la presa e' abbinata nell'app eWeLink?\n"
        "  - questa macchina e la presa sono sulla stessa subnet?\n"
        "  - il firewall di Windows consente il traffico mDNS in ingresso?\n"
        "  - non lascia passare il traffico mDNS (multicast) verso l'host\n"
    )


def discover(seconds: float) -> int:
    #Modalita' di ricognizione

    listener = Listener("", None, lambda row: None, verbose=False, discovery_only=True)
    zc = _browser(listener)
    print(f"In ascolto su {SERVICE} per {seconds:.0f} s...")
    try:
        time.sleep(seconds)
    finally:
        zc.close()

    if not listener.devices:
        print(_diagnosis())
        return 1

    print("\nDispositivi trovati:")
    for device_id, address in sorted(listener.devices.items()):
        print(f"  id={device_id}  ip={address}")
    print("\nUsa l'id con --id, e la devicekey del dispositivo con --key.")
    return 0


def record(devicekey: str, only_id: str | None, output: Path, minutes: float | None,
           verbose: bool, keys: dict[str, str] | None = None) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    is_new = not output.exists() or output.stat().st_size == 0

    with output.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(HEADER)
            f.flush()

        def write(row):
            #flush a ogni riga
            writer.writerow(row)
            f.flush()

        listener = Listener(devicekey, only_id, write, verbose, keys=keys)
        zc = _browser(listener)

        start = time.monotonic()
        end = start + minutes * 60.0 if minutes else None
        warned = False
        print(f"Registrazione su {output}. Ctrl+C per fermare.")

        try:
            while end is None or time.monotonic() < end:
                time.sleep(1.0)
                elapsed = time.monotonic() - start
                if not warned and elapsed > WAIT_BEFORE_DIAGNOSIS:
                    warned = True
                    if not listener.devices:
                        print("\n" + _diagnosis() + "\n", file=sys.stderr)
                    elif listener.readings == 0:
                        print(
                            "\nIl dispositivo si annuncia ma nessuna lettura e' stata\n"
                            "decifrata: la devicekey e' probabilmente sbagliata.\n",
                            file=sys.stderr,
                        )
        except KeyboardInterrupt:
            print("\ninterrotto")
        finally:
            zc.close()

    duration = time.monotonic() - start
    print(
        f"\n{listener.readings} letture in {duration / 60.0:.1f} min "
        f"({listener.errors} record non decifrati) -> {output}"
    )
    if listener.readings == 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    #descrizione esplicita 
    parser = argparse.ArgumentParser(
        description="Registra in CSV la potenza letta in LAN da una presa Sonoff"
    )
    parser.add_argument("--key", default=None, help="devicekey del dispositivo")
    parser.add_argument(
        "--keys",
        default=None,
        help="JSON id->devicekey scritto da device_key.py",
    )
    parser.add_argument("--id", default=None, help="registra solo questo id di dispositivo")
    parser.add_argument(
        "--output",
        default=None,
        help="file CSV, in append; predefinito measurements/plug_<date>.csv",
    )
    parser.add_argument("--minutes", type=float, default=None, help="durata, altrimenti Ctrl+C")
    parser.add_argument(
        "--list",
        action="store_true",
        help="scopre i dispositivi in rete e esce, senza bisogno della devicekey",
    )
    parser.add_argument("--discovery-seconds", type=float, default=15.0)
    parser.add_argument("--verbose", action="store_true", help="stampa ogni lettura")
    args = parser.parse_args(argv)

    if args.list:
        return discover(args.discovery_seconds)

    keys: dict[str, str] = {}
    if args.keys:
        keys_path = Path(args.keys)
        if not keys_path.exists():
            parser.error(f"{keys_path} non esiste: eseguire prima device_key.py")
        keys = json.loads(keys_path.read_text(encoding="utf-8"))

    if not args.key and not keys:
        parser.error(
            "serve --key con la devicekey, oppure --keys col file di device_key.py, "
            "oppure --list per la sola ricognizione"
        )
    if args.minutes is not None and args.minutes <= 0:
        parser.error("--minutes deve essere positivo")

    if args.output:
        output = Path(args.output)
    else:
        output = Path("measurements") / f"plug_{datetime.now(UTC):%Y%m%d_%H%M%S}.csv"

    return record(args.key or "", args.id, output, args.minutes, args.verbose, keys)


if __name__ == "__main__":
    sys.exit(main())
