#Anagrafica dei partecipanti
#Sulla macchina locale dell'universita'
#unico posto in cui un'impronta SHA-256 si traduce in una identità
# La VM conosce solo le impronte
#
#   python registry.py enroll --first-name Mario --last-name Rossi --age 24 --email m.rossi@studenti.unipi.it
#   python registry.py list
#   python registry.py revoke --email m.rossi@studenti.unipi.it
#   python registry.py withdraw --email m.rossi@studenti.unipi.it
#   python registry.py phases --email m.rossi@studenti.unipi.it --baseline 168 --advice 168
#       (la VM rifiuta di spostare un confine gia' passato o a meno di 2 ore)
#   python registry.py pull --output dati.csv
#   python registry.py pull-grid --output rete.csv
#
#Il token in chiaro non viene salvato da nessuna parte: si stampa una volta per
#consegnarlo al partecipante

import argparse
import csv
import hashlib
import io
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SSH_ENV_VAR = "ECOMONITOR_STUDY_SSH"
ADMIN_COMMAND = "ecomonitor-study-admin"
TOKEN_BYTES = 32
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PERSONAL_COLUMNS = ("first_name", "last_name", "age", "email")

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    age INTEGER NOT NULL CHECK (age BETWEEN 14 AND 120),
    email TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE CHECK (length(token_sha256) = 64),
    enrolled_at TEXT NOT NULL,
    revoked_at TEXT
);
--una persona ha un solo token valido alla volta; quelli revocati restano, con i loro dati
CREATE UNIQUE INDEX IF NOT EXISTS one_active_token_per_email
    ON participants (email) WHERE revoked_at IS NULL;
"""

#riceve gli argomenti del comando di amministrazione, restituisce lo stdout
RemoteRunner = Callable[[Sequence[str]], str]


class RemoteError(Exception):
    pass


def default_database_path() -> Path:
    override = os.environ.get("ECOMONITOR_REGISTRY_DB")
    if override:
        return Path(override)
    return Path.home() / "ecomonitor-study" / "registry.db"


def ssh_runner(target: str) -> RemoteRunner:
    def run(arguments: Sequence[str]) -> str:
        #gli argomenti sono impronte esadecimali
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", target, ADMIN_COMMAND, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"codice di uscita {result.returncode}"
            raise RemoteError(f"VM: {detail}")
        return result.stdout

    return run


def open_registry(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hashlib.sha256(token.encode("ascii")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_active(conn: sqlite3.Connection, email: str) -> str:
    row = conn.execute(
        "SELECT token_sha256 FROM participants WHERE email = ? AND revoked_at IS NULL",
        (email.lower(),),
    ).fetchone()
    if row is None:
        raise SystemExit(f"nessun partecipante attivo con mail {email}")
    return row[0]


def enroll(
    conn: sqlite3.Connection,
    remote: RemoteRunner,
    first_name: str,
    last_name: str,
    age: int,
    email: str,
) -> str:
    if EMAIL_PATTERN.fullmatch(email) is None:
        raise SystemExit(f"mail non valida: {email}")
    token, digest = new_token()
    with conn:
        conn.execute(
            "INSERT INTO participants "
            "(first_name, last_name, age, email, token_sha256, enrolled_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (first_name.strip(), last_name.strip(), age, email.lower(), digest, _now()),
        )
        remote(["add", digest])
    return token


def revoke(conn: sqlite3.Connection, remote: RemoteRunner, email: str) -> None:
    digest = _find_active(conn, email)
    try:
        remote(["revoke", digest])
    except RemoteError as error:
        #gia' revocato da un tentativo interrotto a meta': resta da segnarlo qui
        if "revocato" not in str(error):
            raise
    with conn:
        conn.execute(
            "UPDATE participants SET revoked_at = ? WHERE token_sha256 = ?", (_now(), digest)
        )


def withdraw(conn: sqlite3.Connection, remote: RemoteRunner, email: str) -> int:
    #ritiro dal GDPR: via le ore sulla VM e i dati personali qui, per ogni token
    #che la persona ha avuto
    digests = [
        row[0]
        for row in conn.execute(
            "SELECT token_sha256 FROM participants WHERE email = ?", (email.lower(),)
        )
    ]
    if not digests:
        raise SystemExit(f"nessun partecipante con mail {email}")
    for digest in digests:
        try:
            remote(["delete", digest])
        except RemoteError as error:
            if "sconosciuto" not in str(error):
                raise
        with conn:
            conn.execute("DELETE FROM participants WHERE token_sha256 = ?", (digest,))
    return len(digests)


def set_phases(
    conn: sqlite3.Connection,
    remote: RemoteRunner,
    email: str,
    baseline_hours: int,
    advice_hours: int,
) -> str:
    #durata delle due fasi del token attivo; la VM rifiuta valori fuori limite
    digest = _find_active(conn, email)
    return remote(["phases", digest, str(baseline_hours), str(advice_hours)]).strip()


def pull(conn: sqlite3.Connection, remote: RemoteRunner, output: Path) -> tuple[int, int]:
    exported = csv.DictReader(io.StringIO(remote(["export"])))
    people = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT token_sha256, first_name, last_name, age, email FROM participants"
        )
    }
    remote_columns = list(exported.fieldnames or [])
    written = 0
    unknown = 0
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([*PERSONAL_COLUMNS, *remote_columns])
        for row in exported:
            person = people.get(row["token_sha256"])
            if person is None:
                #un'impronta che l'anagrafica non conosce
                unknown += 1
                continue
            writer.writerow([*person, *(row[name] for name in remote_columns)])
            written += 1
    return written, unknown


def pull_grid(remote: RemoteRunner, output: Path) -> int:
    #l'intensita' della rete di ogni ora raccolta: nessun dato personale, quindi
    #niente da unire, si salva com'e'
    text = remote(["export-grid"])
    output.write_text(text, encoding="utf-8", newline="")
    return max(0, len(text.splitlines()) - 1)


def main(argv: list[str] | None = None, remote: RemoteRunner | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anagrafica dello studio EcoMonitor.")
    parser.add_argument("--db", type=Path, default=None, help="file SQLite dell'anagrafica")
    parser.add_argument(
        "--ssh",
        default=os.environ.get(SSH_ENV_VAR),
        help=f"utente@host della VM, oppure la variabile {SSH_ENV_VAR}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    enroll_parser = commands.add_parser("enroll", help="registra un partecipante")
    enroll_parser.add_argument("--first-name", required=True)
    enroll_parser.add_argument("--last-name", required=True)
    enroll_parser.add_argument("--age", type=int, required=True)
    enroll_parser.add_argument("--email", required=True)

    commands.add_parser("list", help="elenca i partecipanti")
    for name, text in (
        ("revoke", "il token smette di valere, i dati restano"),
        ("withdraw", "ritiro: cancella i dati sulla VM e quelli personali qui"),
    ):
        commands.add_parser(name, help=text).add_argument("--email", required=True)

    phases_parser = commands.add_parser("phases", help="durata in ore delle due fasi")
    phases_parser.add_argument("--email", required=True)
    phases_parser.add_argument("--baseline", type=int, required=True, help="ore di fase 1")
    phases_parser.add_argument("--advice", type=int, required=True, help="ore di fase 2")

    pull_parser = commands.add_parser("pull", help="scarica le ore e le unisce all'anagrafica")
    pull_parser.add_argument("--output", type=Path, required=True)

    grid_parser = commands.add_parser(
        "pull-grid", help="scarica l'intensita' della rete di ogni ora, per zona"
    )
    grid_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    #l'elenco non esce da questa macchina
    if remote is None and not args.ssh and args.command != "list":
        parser.error(f"manca la VM: --ssh utente@host oppure {SSH_ENV_VAR}")
    runner = remote if remote is not None else ssh_runner(args.ssh or "")

    with closing(open_registry(args.db or default_database_path())) as conn:
        try:
            if args.command == "enroll":
                token = enroll(
                    conn, runner, args.first_name, args.last_name, args.age, args.email
                )
                print("Token da consegnare al partecipante (non viene salvato):")
                print(token)
            elif args.command == "list":
                writer = csv.writer(sys.stdout, lineterminator="\n")
                writer.writerow(
                    (*PERSONAL_COLUMNS, "token_sha256", "enrolled_at", "revoked_at")
                )
                writer.writerows(
                    conn.execute(
                        "SELECT first_name, last_name, age, email, token_sha256, "
                        "enrolled_at, coalesce(revoked_at, '') "
                        "FROM participants ORDER BY enrolled_at"
                    )
                )
            elif args.command == "revoke":
                revoke(conn, runner, args.email)
                print("revocato")
            elif args.command == "withdraw":
                count = withdraw(conn, runner, args.email)
                print(f"cancellato ({count} token)")
            elif args.command == "phases":
                print(set_phases(conn, runner, args.email, args.baseline, args.advice))
            elif args.command == "pull-grid":
                count = pull_grid(runner, args.output)
                print(f"{count} ore di rete scritte in {args.output}")
            else:
                written, unknown = pull(conn, runner, args.output)
                print(f"{written} ore scritte in {args.output}")
                if unknown:
                    print(f"ATTENZIONE: {unknown} ore di impronte assenti dall'anagrafica")
        except RemoteError as error:
            print(error, file=sys.stderr)
            return 1
        except sqlite3.IntegrityError as error:
            #mail gia' attiva, o eta' fuori dai limiti dello schema
            print(f"anagrafica: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
