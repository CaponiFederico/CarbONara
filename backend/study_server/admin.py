#Amministrazione dello studio
#
#   ecomonitor-study-admin add <sha256>       registra un partecipante
#   ecomonitor-study-admin revoke <sha256>    il token smette di valere, i dati restano
#   ecomonitor-study-admin delete <sha256>    ritiro: cancella partecipante e ore
#   ecomonitor-study-admin list               impronte, adesione, revoca, ore ricevute
#   ecomonitor-study-admin export             tutte le ore in CSV su stdout, con
#                                             l'intensita' della rete di quell'ora
#   ecomonitor-study-admin export-grid        l'intensita' della rete di ogni ora
#                                             raccolta, anche a computer spento
#   ecomonitor-study-admin grid-status        copertura per zona e richieste spese

import argparse
import csv
import os
import re
import sys

import psycopg

from . import grid

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

EXPORT_COLUMNS = (
    "token_sha256",
    "hour_start",
    "received_at",
    "schema_version",
    "app_version",
    "os_family",
    "device_class",
    "country_code",
    "zone",
    "covered_seconds",
    "energy_wh",
    "gco2",
    "estimated_wh",
    "battery_wh",
    "fallback_wh",
    "cpu_wh",
    "grid_g_per_kwh",
)

#l'utc scritto come nel resto delle esportazioni
def _utc(column: str) -> str:
    return f"to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"


EXPORT_QUERY = (
    f"SELECT p.token_sha256, {_utc('h.hour_start')}, {_utc('h.received_at')}, "
    + ", ".join(f"h.{name}" for name in EXPORT_COLUMNS[3:-1])
    + ", g.g_per_kwh"
    " FROM hourly_usage h JOIN participants p ON p.id = h.participant_id"
    " LEFT JOIN grid_hourly g ON g.zone = h.zone AND g.hour_start = h.hour_start"
    " ORDER BY p.token_sha256, h.hour_start"
)

GRID_COLUMNS = (
    "zone",
    "hour_start",
    "g_per_kwh",
    "is_final",
    "first_g_per_kwh",
    "revisions",
    "is_estimated",
    "estimation_method",
    "source_updated_at",
    "fetched_at",
    "fetched_after_hours",
)

#definitiva: il raccoglitore non la richiede piu'
FINAL_AFTER_HOURS = grid.SETTLE_HOURS + grid.REVISION_HOURS

GRID_QUERY = (
    f"SELECT zone, {_utc('hour_start')}, g_per_kwh, "
    f"hour_start < now() - interval '{FINAL_AFTER_HOURS} hours', "
    f"first_g_per_kwh, revisions, is_estimated, estimation_method, "
    f"{_utc('source_updated_at')}, {_utc('fetched_at')}, "
    "round(extract(epoch FROM fetched_at - hour_start) / 3600) "
    "FROM grid_hourly ORDER BY zone, hour_start"
)

GRID_STATUS_QUERY = (
    "SELECT u.zone, min(u.hour_start), max(u.hour_start), "
    "(SELECT count(*) FROM grid_hourly g WHERE g.zone = u.zone AND g.g_per_kwh IS NOT NULL), "
    "(SELECT count(*) FROM grid_hourly g WHERE g.zone = u.zone AND g.g_per_kwh IS NULL), "
    "(SELECT max(g.hour_start) FROM grid_hourly g WHERE g.zone = u.zone), "
    "(SELECT count(*) FROM grid_requests r "
    "WHERE r.zone = u.zone AND r.requested_at > now() - interval '1 day') "
    "FROM hourly_usage u GROUP BY u.zone ORDER BY u.zone"
)


def _digest(value: str) -> str:
    if SHA256_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("servono 64 cifre esadecimali minuscole")
    return value


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("ECOMONITOR_STUDY_DSN", "dbname=ecomonitor_study"))


def _set_role(conn: psycopg.Connection) -> None:
    conn.execute("SET ROLE ecomonitor_admin")


def cmd_add(digest: str) -> int:
    with _connect() as conn:
        _set_role(conn)
        row = conn.execute(
            "INSERT INTO participants (token_sha256) VALUES (%s) "
            "ON CONFLICT (token_sha256) DO NOTHING RETURNING id",
            (digest,),
        ).fetchone()
    if row is None:
        print("gia' registrato", file=sys.stderr)
        return 1
    print("registrato")
    return 0


def cmd_revoke(digest: str) -> int:
    with _connect() as conn:
        _set_role(conn)
        count = conn.execute(
            "UPDATE participants SET revoked_at = now() "
            "WHERE token_sha256 = %s AND revoked_at IS NULL",
            (digest,),
        ).rowcount
    if count == 0:
        print("sconosciuto o gia' revocato", file=sys.stderr)
        return 1
    print("revocato")
    return 0


def cmd_delete(digest: str) -> int:
    with _connect() as conn:
        _set_role(conn)
        count = conn.execute(
            "DELETE FROM participants WHERE token_sha256 = %s", (digest,)
        ).rowcount
    if count == 0:
        print("sconosciuto", file=sys.stderr)
        return 1
    print("cancellato, con tutte le sue ore")
    return 0


def cmd_list() -> int:
    with _connect() as conn:
        _set_role(conn)
        rows = conn.execute(
            "SELECT p.token_sha256, p.joined_at, p.revoked_at, count(h.hour_start) "
            "FROM participants p LEFT JOIN hourly_usage h ON h.participant_id = p.id "
            "GROUP BY p.id ORDER BY p.joined_at"
        ).fetchall()
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(("token_sha256", "joined_at", "revoked_at", "hours"))
    for digest, joined, revoked, hours in rows:
        writer.writerow(
            (digest, joined.isoformat(), revoked.isoformat() if revoked else "", hours)
        )
    return 0


def cmd_export() -> int:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    with _connect() as conn:
        _set_role(conn)
        #cursore lato server: l'esportazione non passa tutta dalla memoria
        with conn.cursor(name="export") as cursor:
            cursor.execute(EXPORT_QUERY)
            for row in cursor:
                writer.writerow(row)
    return 0


def cmd_export_grid() -> int:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(GRID_COLUMNS)
    with _connect() as conn:
        _set_role(conn)
        with conn.cursor(name="export_grid") as cursor:
            cursor.execute(GRID_QUERY)
            for row in cursor:
                writer.writerow(row)
    return 0


def cmd_grid_status() -> int:
    with _connect() as conn:
        _set_role(conn)
        rows = conn.execute(GRID_STATUS_QUERY).fetchall()
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(
        (
            "zone",
            "first_usage_hour",
            "last_usage_hour",
            "grid_hours",
            "grid_hours_unavailable",
            "last_grid_hour",
            "requests_last_24h",
        )
    )
    for zone, first, last, known, missing, last_grid, requests in rows:
        writer.writerow(
            (
                zone,
                first.isoformat(),
                last.isoformat(),
                known,
                missing,
                last_grid.isoformat() if last_grid else "",
                requests,
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecomonitor-study-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "revoke", "delete"):
        commands.add_parser(name).add_argument("digest", type=_digest)
    commands.add_parser("list")
    commands.add_parser("export")
    commands.add_parser("export-grid")
    commands.add_parser("grid-status")
    args = parser.parse_args(argv)

    if args.command == "add":
        return cmd_add(args.digest)
    if args.command == "revoke":
        return cmd_revoke(args.digest)
    if args.command == "delete":
        return cmd_delete(args.digest)
    if args.command == "list":
        return cmd_list()
    if args.command == "export-grid":
        return cmd_export_grid()
    if args.command == "grid-status":
        return cmd_grid_status()
    return cmd_export()


if __name__ == "__main__":
    raise SystemExit(main())
