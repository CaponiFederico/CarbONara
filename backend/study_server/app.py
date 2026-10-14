#API REST dello studio
#
#   POST /v1/hours    invia ore di consumo
#   GET  /v1/status   ultima ora ricevuta, limiti da rispettare e fase dello studio
#   GET  /healthz     il database risponde

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import validation
from .phases import Calendar, calendar, phase_at
from .store import Participant, PostgresStore, Store, study_start

log = logging.getLogger("study_server")

#secrets.token_urlsafe(32) produce 43 caratteri url-safe
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
UNAVAILABLE = "database non disponibile, riprovare piu' tardi"


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _error(status: int, error: str, **details: Any) -> JSONResponse:
    return JSONResponse({"error": error, **details}, status_code=status)


def _utc_text(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _study(dates: Calendar, now: datetime) -> dict[str, Any]:
    #la fase la decide il server
    #le date bastano all'app per cambiarla anche senza rete
    return {
        "phase": phase_at(dates, now),
        "advice_starts_at": _utc_text(dates.advice_starts_at),
        "ends_at": _utc_text(dates.ends_at),
    }


def create_app(store: Store, clock=lambda: datetime.now(UTC)) -> FastAPI:
    app = FastAPI(title="EcoMonitor study", docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(request: Request) -> Participant | None | JSONResponse:
        match = re.fullmatch(r"Bearer (\S+)", request.headers.get("authorization", ""))
        if match is None or TOKEN_PATTERN.fullmatch(match[1]) is None:
            return None
        try:
            return store.find_participant(token_sha256(match[1]))
        except Exception:
            log.exception("autenticazione non riuscita")
            return _error(503, UNAVAILABLE)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            store.find_participant("0" * 64)
        except Exception:
            log.exception("database non raggiungibile")
            return _error(503, UNAVAILABLE)
        return JSONResponse({"ok": True})

    @app.get("/v1/status")
    def status(request: Request) -> JSONResponse:
        participant = authenticate(request)
        if isinstance(participant, JSONResponse):
            return participant
        if participant is None:
            return _error(401, "token mancante, sconosciuto o revocato")
        try:
            last = store.last_hour(participant.id)
        except Exception:
            log.exception("lettura non riuscita")
            return _error(503, UNAVAILABLE)
        return JSONResponse(
            {
                "joined_at": _utc_text(participant.joined_at),
                "last_hour_start": _utc_text(last),
                "limits": validation.limits(),
                "study": _study(participant.calendar, clock()),
            }
        )

    #async per leggere il corpo; Postgres e' sincrono e va nel pool di thread,
    #altrimenti un invio lento fermerebbe tutte le altre richieste del worker
    @app.post("/v1/hours")
    async def hours(request: Request) -> JSONResponse:
        participant = await run_in_threadpool(authenticate, request)
        if isinstance(participant, JSONResponse):
            return participant
        if participant is None:
            return _error(401, "token mancante, sconosciuto o revocato")

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > validation.MAX_BODY_BYTES:
            return _error(413, "invio troppo grande")
        raw = await request.body()
        if len(raw) > validation.MAX_BODY_BYTES:
            return _error(413, "invio troppo grande")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            #ValueError: anche un intero con troppe cifre; RecursionError: troppi livelli
            return _error(400, "il corpo non e' JSON valido")

        try:
            submission = validation.validate_submission(
                body, clock(), participant.joined_at, participant.calendar.ends_at
            )
        except validation.ValidationError as rejection:
            return JSONResponse(rejection.as_dict(), status_code=400)

        try:
            await run_in_threadpool(store.save, participant.id, submission)
        except Exception:
            log.exception("scrittura non riuscita")
            return _error(503, UNAVAILABLE)
        #il primo invio fissa il calendario
        started_at = participant.started_at or study_start(submission)
        dates = calendar(started_at, participant.baseline_hours, participant.advice_hours)
        return JSONResponse(
            {"accepted": len(submission.hours), "study": _study(dates, clock())}
        )

    return app


def app_from_environment() -> FastAPI:
    #la connessione passa dal socket locale con l'autenticazione peer: nessuna password
    return create_app(PostgresStore(os.environ.get("ECOMONITOR_STUDY_DSN", "dbname=ecomonitor_study")))
