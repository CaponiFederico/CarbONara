#Fasi dello studio, dal calendario del partecipante
#
#   fase 1, baseline: l'app raccoglie senza mostrare consigli
#   fase 2, advice:   l'app completa
#   ended:            lo studio per quella persona e' finito
#
#Il calendario parte da started_at, la prima ora intera dopo la verifica del codice
#nell'app (POST /v1/start)

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

BASELINE = "baseline"
ADVICE = "advice"
ENDED = "ended"
#prima che arrivi una sola ora in modalita' studio
BEFORE = "before"

#le fasi che un'ora puo' dichiarare
HOUR_PHASES = frozenset({BASELINE, ADVICE})

DEFAULT_BASELINE_HOURS = 168
DEFAULT_ADVICE_HOURS = 168
MAX_PHASE_HOURS = 24 * 90

CHANGE_NOTICE_HOURS = 2


@dataclass(frozen=True)
class Calendar:
    advice_starts_at: datetime | None
    ends_at: datetime | None
    starts_at: datetime | None = None


def calendar(started_at: datetime | None, baseline_hours: int, advice_hours: int) -> Calendar:
    if started_at is None:
        return Calendar(None, None)
    advice_starts_at = started_at + timedelta(hours=baseline_hours)
    return Calendar(advice_starts_at, advice_starts_at + timedelta(hours=advice_hours), started_at)


def next_whole_hour(moment: datetime) -> datetime:
    floor = moment.replace(minute=0, second=0, microsecond=0)
    return floor if floor == moment else floor + timedelta(hours=1)


def phase_at(dates: Calendar, moment: datetime) -> str:
    #senza date si e' in fase 1: in dubbio niente consigli
    if dates.advice_starts_at is None or moment < dates.advice_starts_at:
        return BASELINE
    if dates.ends_at is None or moment < dates.ends_at:
        return ADVICE
    return ENDED


def change_refusal(
    started_at: datetime | None,
    current: tuple[int, int],
    wanted: tuple[int, int],
    now: datetime,
) -> str | None:
    #None se le durate si possono cambiare
    #un confine gia' passato non si sposta: le ore di prima cambierebbero fase
    #rispetto a quella che il partecipante ha vissuto
    if started_at is None:
        return None
    before = calendar(started_at, *current)
    after = calendar(started_at, *wanted)
    limit = now + timedelta(hours=CHANGE_NOTICE_HOURS)
    for name, old, new in (
        ("l'inizio della fase 2", before.advice_starts_at, after.advice_starts_at),
        ("la fine dello studio", before.ends_at, after.ends_at),
    ):
        if old is None or new is None or old == new:
            continue
        if old < limit:
            return (
                f"{name} ({_utc_text(old)}) e' gia' passato o manca meno di "
                f"{CHANGE_NOTICE_HOURS} ore: non si puo' spostare"
            )
        if new < limit:
            return (
                f"{name} cadrebbe il {_utc_text(new)}: serve un preavviso di almeno "
                f"{CHANGE_NOTICE_HOURS} ore, cioe' non prima del {_utc_text(limit)}"
            )
    return None


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
