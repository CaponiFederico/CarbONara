#API REST dello studio
#
#   POST /v1/start    verifica il codice e fissa l'inizio del calendario
#   POST /v1/hours    invia ore di consumo
#   GET  /v1/status   ultima ora ricevuta dal dispositivo, limiti e fase dello studio
#   GET  /v1/grid/latest?zone=            intensita' della rete adesso
#   GET  /v1/grid/forecast?zone=          previsione oraria
#   GET  /v1/grid/past?zone=&start=&end=  storico orario
#   GET  /healthz     il database risponde

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import grid_feed, validation
from .grid_feed import GridReader, PostgresGridReader
from .phases import Calendar, calendar, next_whole_hour, phase_at
from .store import Participant, PostgresStore, Store, study_start
from .zones import SERVED_ZONES

log = logging.getLogger("study_server")

#secrets.token_urlsafe(32) produce 43 caratteri url-safe
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
UNAVAILABLE = "database non disponibile, riprovare piu' tardi"
GRID_NOT_READY = "dati di rete non disponibili per questa zona, riprovare piu' tardi"
#il dispositivo che chiede lo stato: ogni dispositivo ha il suo progresso
INSTALL_ID_HEADER = "x-install-id"
HOUR = timedelta(hours=1)


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
        "starts_at": _utc_text(dates.starts_at),
        "advice_starts_at": _utc_text(dates.advice_starts_at),
        "ends_at": _utc_text(dates.ends_at),
    }


def create_app(
    store: Store, clock=lambda: datetime.now(UTC), grid: GridReader | None = None
) -> FastAPI:
    app = FastAPI(title="EcoMonitor study", docs_url=None, redoc_url=None, openapi_url=None)

    def grid_for(zone: str) -> GridReader | JSONResponse:
        #ogni richiesta annota la zona: e' cosi' che il raccoglitore sa cosa leggere
        if zone not in SERVED_ZONES:
            return _error(404, "zona non servita", field="zone")
        if grid is None:
            return _error(503, GRID_NOT_READY)
        try:
            grid.note_demand(zone)
        except Exception:
            log.exception("domanda della zona non annotata")
            return _error(503, UNAVAILABLE)
        return grid

    def read_snapshot(zone: str, kind: str) -> grid_feed.Snapshot | None | JSONResponse:
        reader = grid_for(zone)
        if isinstance(reader, JSONResponse):
            return reader
        try:
            return reader.snapshot(zone, kind)
        except Exception:
            log.exception("lettura dei dati di rete non riuscita")
            return _error(503, UNAVAILABLE)

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
        install_id = request.headers.get(INSTALL_ID_HEADER, "")
        if validation.INSTALL_ID_PATTERN.fullmatch(install_id) is None:
            return _error(
                400, "identificativo dell'installazione mancante o non valido", field="install_id"
            )
        try:
            last = store.last_hour(participant.id, install_id)
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

    #l'app la chiama quando il partecipante scrive il codice: un codice sbagliato si
    #scopre subito, e da qui in poi l'app conosce le date anche senza rete
    @app.post("/v1/start")
    def start(request: Request) -> JSONResponse:
        participant = authenticate(request)
        if isinstance(participant, JSONResponse):
            return participant
        if participant is None:
            return _error(401, "token mancante, sconosciuto o revocato")
        now = clock()
        try:
            started_at = store.start_study(participant.id, next_whole_hour(now))
        except Exception:
            log.exception("inizio dello studio non registrato")
            return _error(503, UNAVAILABLE)
        dates = calendar(started_at, participant.baseline_hours, participant.advice_hours)
        return JSONResponse({"study": _study(dates, now)})

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

    @app.get("/v1/grid/latest")
    def grid_latest(zone: str = "") -> JSONResponse:
        snapshot = read_snapshot(zone, "latest")
        if isinstance(snapshot, JSONResponse):
            return snapshot
        if snapshot is None or not grid_feed.is_fresh(
            snapshot, grid_feed.LATEST_MAX_AGE, clock()
        ):
            return _error(503, GRID_NOT_READY)
        point = snapshot.points[-1]
        return JSONResponse(
            {
                "zone": zone,
                "datetime": point["datetime"],
                "g_per_kwh": point["g_per_kwh"],
                "fetched_at": _utc_text(snapshot.fetched_at),
            }
        )

    @app.get("/v1/grid/forecast")
    def grid_forecast(zone: str = "") -> JSONResponse:
        snapshot = read_snapshot(zone, "forecast")
        if isinstance(snapshot, JSONResponse):
            return snapshot
        if snapshot is None or not grid_feed.is_fresh(
            snapshot, grid_feed.FORECAST_MAX_AGE, clock()
        ):
            return _error(503, GRID_NOT_READY)
        return JSONResponse(
            {
                "zone": zone,
                "forecast": snapshot.points,
                "fetched_at": _utc_text(snapshot.fetched_at),
            }
        )

    @app.get("/v1/grid/past")
    def grid_past(zone: str = "", start: str = "", end: str = "") -> JSONResponse:
        window_start = grid_feed.parse_utc(start)
        window_end = grid_feed.parse_utc(end)
        if window_start is None:
            return _error(400, "inizio mancante o senza fuso orario", field="start")
        if window_end is None or window_end <= window_start:
            return _error(400, "fine mancante, senza fuso orario o prima dell'inizio", field="end")
        if window_end - window_start > grid_feed.MAX_PAST_HOURS * HOUR:
            return _error(
                400,
                f"al massimo {grid_feed.MAX_PAST_HOURS} ore per richiesta",
                field="end",
                max_hours=grid_feed.MAX_PAST_HOURS,
            )
        reader = grid_for(zone)
        if isinstance(reader, JSONResponse):
            return reader
        try:
            settled = reader.settled_hours(zone, window_start, window_end)
            recent = reader.snapshot(zone, "recent")
        except Exception:
            log.exception("lettura dello storico di rete non riuscita")
            return _error(503, UNAVAILABLE)
        return JSONResponse(
            {
                "zone": zone,
                "data": grid_feed.past_points(settled, recent, window_start, window_end),
            }
        )

    return app


def app_from_environment() -> FastAPI:
    #la connessione passa dal socket locale con l'autenticazione peer: nessuna password
    dsn = os.environ.get("ECOMONITOR_STUDY_DSN", "dbname=ecomonitor_study")
    return create_app(PostgresStore(dsn), grid=PostgresGridReader(dsn))
