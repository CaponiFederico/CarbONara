#Accesso al database

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg

from .phases import DEFAULT_ADVICE_HOURS, DEFAULT_BASELINE_HOURS, Calendar, calendar
from .validation import HOUR_NUMBER_FIELDS, Submission

SUBMISSION_COLUMNS = (
    "schema_version",
    "app_version",
    "install_id",
    "os_family",
    "device_class",
    "country_code",
    "zone",
)
HOUR_COLUMNS = ("phase", *HOUR_NUMBER_FIELDS)
STORED_COLUMNS = (*SUBMISSION_COLUMNS, *HOUR_COLUMNS)

UPSERT_HOUR = (
    "INSERT INTO hourly_usage (participant_id, hour_start, received_at, "
    + ", ".join(STORED_COLUMNS)
    + ") VALUES (%s, %s, now(), "
    + ", ".join(["%s"] * len(STORED_COLUMNS))
    + ") ON CONFLICT (participant_id, install_id, hour_start) DO UPDATE SET "
    + "received_at = excluded.received_at, "
    + ", ".join(f"{name} = excluded.{name}" for name in STORED_COLUMNS)
)

#le fasi partono dalla prima ora inviata in modalita' studio
START_STUDY = (
    "UPDATE participants SET started_at = %s WHERE id = %s AND started_at IS NULL"
)


@dataclass(frozen=True)
class Participant:
    id: int
    joined_at: datetime
    started_at: datetime | None = None
    baseline_hours: int = DEFAULT_BASELINE_HOURS
    advice_hours: int = DEFAULT_ADVICE_HOURS

    @property
    def calendar(self) -> Calendar:
        return calendar(self.started_at, self.baseline_hours, self.advice_hours)


def study_start(submission: Submission) -> datetime | None:
    starts = [hour.hour_start for hour in submission.hours if hour.phase is not None]
    return min(starts, default=None)


class Store(Protocol):
    def find_participant(self, token_sha256: str) -> Participant | None: ...

    def last_hour(self, participant_id: int, install_id: str) -> datetime | None: ...

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
                "SELECT id, joined_at, started_at, baseline_hours, advice_hours "
                "FROM participants WHERE token_sha256 = %s AND revoked_at IS NULL",
                (token_sha256,),
            ).fetchone()
        if row is None:
            return None
        return Participant(
            id=row[0],
            joined_at=row[1],
            started_at=row[2],
            baseline_hours=row[3],
            advice_hours=row[4],
        )

    def last_hour(self, participant_id: int, install_id: str) -> datetime | None:
        #il progresso e' per dispositivo: ognuno manda le sue ore
        with self._connect() as conn:
            row = conn.execute(
                "SELECT max(hour_start) FROM hourly_usage "
                "WHERE participant_id = %s AND install_id = %s",
                (participant_id, uuid.UUID(install_id)),
            ).fetchone()
        return None if row is None else row[0]

    def save(self, participant_id: int, submission: Submission) -> None:
        envelope = (
            submission.schema_version,
            submission.app_version,
            uuid.UUID(submission.install_id),
            submission.os_family,
            submission.device_class,
            submission.country_code,
            submission.zone,
        )
        #una transazione sola: o entra tutto l'invio o niente
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.executemany(
                UPSERT_HOUR,
                [
                    (
                        participant_id,
                        hour.hour_start,
                        *envelope,
                        *(getattr(hour, name) for name in HOUR_COLUMNS),
                    )
                    for hour in submission.hours
                ],
            )
            start = study_start(submission)
            if start is not None:
                cursor.execute(START_STUDY, (start, participant_id))
