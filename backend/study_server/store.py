#Accesso al database

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg

from .validation import Submission

UPSERT_HOUR = """
INSERT INTO hourly_usage (
    participant_id, hour_start, received_at, schema_version, app_version,
    os_family, device_class, country_code, zone, covered_seconds,
    energy_wh, gco2, estimated_wh, battery_wh, fallback_wh, cpu_wh
) VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (participant_id, hour_start) DO UPDATE SET
    received_at = excluded.received_at,
    schema_version = excluded.schema_version,
    app_version = excluded.app_version,
    os_family = excluded.os_family,
    device_class = excluded.device_class,
    country_code = excluded.country_code,
    zone = excluded.zone,
    covered_seconds = excluded.covered_seconds,
    energy_wh = excluded.energy_wh,
    gco2 = excluded.gco2,
    estimated_wh = excluded.estimated_wh,
    battery_wh = excluded.battery_wh,
    fallback_wh = excluded.fallback_wh,
    cpu_wh = excluded.cpu_wh
"""


@dataclass(frozen=True)
class Participant:
    id: int
    joined_at: datetime


class Store(Protocol):
    def find_participant(self, token_sha256: str) -> Participant | None: ...

    def last_hour(self, participant_id: int) -> datetime | None: ...

    def save(self, participant_id: int, submission: Submission) -> None: ...


class PostgresStore:
    #una connessione per richiesta
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def find_participant(self, token_sha256: str) -> Participant | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, joined_at FROM participants "
                "WHERE token_sha256 = %s AND revoked_at IS NULL",
                (token_sha256,),
            ).fetchone()
        return None if row is None else Participant(id=row[0], joined_at=row[1])

    def last_hour(self, participant_id: int) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT max(hour_start) FROM hourly_usage WHERE participant_id = %s",
                (participant_id,),
            ).fetchone()
        return None if row is None else row[0]

    def save(self, participant_id: int, submission: Submission) -> None:
        #una transazione sola: o entra tutto l'invio o niente
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.executemany(
                UPSERT_HOUR,
                [
                    (
                        participant_id,
                        hour.hour_start,
                        submission.schema_version,
                        submission.app_version,
                        submission.os_family,
                        submission.device_class,
                        submission.country_code,
                        submission.zone,
                        hour.covered_seconds,
                        hour.energy_wh,
                        hour.gco2,
                        hour.estimated_wh,
                        hour.battery_wh,
                        hour.fallback_wh,
                        hour.cpu_wh,
                    )
                    for hour in submission.hours
                ],
            )
