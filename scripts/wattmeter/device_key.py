# Recupero della devicekey dei dispositivi eWeLink
#
# Serve una volta sola
#
#     python scripts/wattmeter/device_key.py
#
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

#Un dispositivo appartiene a una regione
API = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}

AUTH_PAGE = "https://c2ccdn.coolkit.cc/oauth/index.html"

TIMEOUT = 20.0

#Quanto si aspetta che l'utente completi il login nel browser
CALLBACK_TIMEOUT = 300.0


def signature(body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _headers(app_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-CK-Appid": app_id,
        "X-CK-Nonce": base64.b32encode(os.urandom(5)).decode()[:8],
    }


def login_url(app_id: str, app_secret: str, redirect_url: str, state: str) -> str:
    seq = str(int(time.time() * 1000))
    params = {
        "clientId": app_id,
        "redirectUrl": redirect_url,
        "grantType": "authorization_code",
        "state": state,
        "nonce": secrets.token_hex(4),
        "seq": seq,
        "authorization": signature(f"{app_id}_{seq}".encode(), app_secret),
    }
    return AUTH_PAGE + "?" + urlencode(params)


class _CallbackHandler(BaseHTTPRequestHandler):
    #Riceve il redirect di eWeLink e ne estrae code, state e regione

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        code = query.get("code", [""])[0]

        if not code:
            #il browser chiede anche /favicon.ico: non e' il redirect
            self.send_response(404)
            self.end_headers()
            return

        self.server.result = {  # type: ignore[attr-defined]
            "code": code,
            "state": query.get("state", [""])[0],
            "region": query.get("region", [""])[0],
        }
        body = (
            b"<html><body style='font-family:sans-serif'>"
            b"<h3>Autorizzazione ricevuta.</h3>"
            b"<p>Puoi chiudere questa scheda e tornare al terminale.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        #il log di default sporca il terminale a ogni richiesta del browser
        pass


def wait_for_code(redirect_url: str, state: str) -> dict[str, str]:
    #Apre la pagina di autorizzazione e resta in ascolto sul redirect

    parsed = urlparse(redirect_url)
    host, port = parsed.hostname or "localhost", parsed.port or 80

    server = HTTPServer((host, port), _CallbackHandler)
    server.result = None  # type: ignore[attr-defined]
    server.timeout = 5.0

    deadline = time.monotonic() + CALLBACK_TIMEOUT
    try:
        while server.result is None:  # type: ignore[attr-defined]
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "nessun redirect ricevuto entro il tempo massimo: il login nel "
                    "browser non e' stato completato, oppure la Redirect URL "
                    "registrata sul portale non coincide con " + redirect_url
                )
            server.handle_request()
    finally:
        server.server_close()

    result: dict[str, str] = server.result  # type: ignore[attr-defined]

    if result["state"] != state:
        raise RuntimeError("state non corrispondente: redirect non riconosciuto")

    return result


def exchange_code(region: str, code: str, redirect_url: str,
                  app_id: str, app_secret: str) -> str:
    #Scambia il codice di autorizzazione con il token di accesso

    body = json.dumps(
        {"redirectUrl": redirect_url, "code": code, "grantType": "authorization_code"},
        separators=(",", ":"),
    ).encode()

    headers = _headers(app_id)
    headers["Authorization"] = "Sign " + signature(body, app_secret)

    response = httpx.post(
        API[region] + "/v2/user/oauth/token",
        content=body,
        headers=headers,
        timeout=TIMEOUT,
    )
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"scambio del codice rifiutato (error {data['error']}): {data.get('msg')}")

    token = (data.get("data") or {}).get("accessToken")
    if not token:
        raise RuntimeError(f"risposta senza accessToken: {data}")
    return token


def list_devices(region: str, token: str, app_id: str) -> list[dict]:
    headers = _headers(app_id)
    headers["Authorization"] = "Bearer " + token

    response = httpx.get(
        API[region] + "/v2/device/thing",
        params={"num": 0},
        headers=headers,
        timeout=TIMEOUT,
    )
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"elenco dispositivi rifiutato (error {data['error']}): {data.get('msg')}")

    return [
        entry["itemData"]
        for entry in data["data"]["thingList"]
        if "deviceid" in entry.get("itemData", {})
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recupera dal cloud eWeLink la devicekey dei propri dispositivi"
    )
    parser.add_argument(
        "--redirect-url",
        default="http://localhost:8000/callback",
        help="deve combaciare con la Redirect URL registrata sul portale",
    )
    parser.add_argument(
        "--region",
        default="eu",
        choices=sorted(API),
        help="usata solo se il redirect non dichiara la regione",
    )
    parser.add_argument("--appid", default=os.environ.get("EWELINK_APPID"))
    parser.add_argument("--appsecret", default=os.environ.get("EWELINK_APPSECRET"))
    parser.add_argument(
        "--output",
        default="measurements/keys.json",
        help="dove salvare le devicekey; '-' per non salvare nulla",
    )
    args = parser.parse_args(argv)

    if not args.appid or not args.appsecret:
        parser.error(
            "servono --appid e --appsecret (o EWELINK_APPID e EWELINK_APPSECRET).\n"
            "Si ottengono creando un'applicazione su dev.ewelink.cc: sono le\n"
            "credenziali dell'applicazione, non quelle del tuo account."
        )

    state = secrets.token_urlsafe(16)
    url = login_url(args.appid, args.appsecret, args.redirect_url, state)

    print("Apro la pagina di autorizzazione eWeLink nel browser.")
    print("Se non si apre, incolla questo indirizzo:\n")
    print(url + "\n")
    webbrowser.open(url)
    print(f"In attesa del redirect su {args.redirect_url} ...")

    try:
        callback = wait_for_code(args.redirect_url, state)
        region = callback["region"] if callback["region"] in API else args.region
        token = exchange_code(region, callback["code"], args.redirect_url,
                              args.appid, args.appsecret)
        devices = list_devices(region, token, args.appid)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"impossibile ascoltare su {args.redirect_url}: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"errore di rete verso l'API eWeLink: {error}", file=sys.stderr)
        return 1

    if not devices:
        print("L'account non contiene dispositivi.", file=sys.stderr)
        return 1

    keys = {}
    print(f"\n{len(devices)} dispositivi:\n")
    for device in devices:
        device_id = device["deviceid"]
        devicekey = device.get("devicekey")
        name = device.get("name") or "senza nome"
        model = (device.get("extra") or {}).get("model") or ""
        #un dispositivo condiviso da un altro account non espone la chiave: la
        #modalita' locale su quel dispositivo non e' possibile
        status = devicekey or "assente (dispositivo condiviso, non di proprieta')"
        print(f"  {device_id}  {name} {model}")
        print(f"    devicekey: {status}")
        if devicekey:
            keys[device_id] = devicekey

    if args.output != "-" and keys:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(keys, indent=2), encoding="utf-8")
        print(f"\nChiavi salvate in {destination}")
        print("Contiene segreti dei tuoi dispositivi: tienilo fuori dal repository.")
        print(
            f"\n  python scripts/wattmeter/read_plug.py --keys {destination} "
            "--output measurements/plug.csv"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
