#Fasi dello studio, dal calendario del partecipante
#
#   fase 1, baseline: l'app raccoglie senza mostrare consigli
#   fase 2, advice:   l'app completa
#   ended:            lo studio per quella persona e' finito
#
#Il calendario parte da started_at, l'inizio della prima ora che l'app ha
#inviato in modalita' studio

from dataclasses import dataclass
from datetime import datetime, timedelta

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


@dataclass(frozen=True)
class Calendar:
    advice_starts_at: datetime | None
    ends_at: datetime | None


def calendar(started_at: datetime | None, baseline_hours: int, advice_hours: int) -> Calendar:
    if started_at is None:
        return Calendar(None, None)
    advice_starts_at = started_at + timedelta(hours=baseline_hours)
    return Calendar(advice_starts_at, advice_starts_at + timedelta(hours=advice_hours))


def phase_at(dates: Calendar, moment: datetime) -> str:
    #senza date si e' in fase 1: in dubbio niente consigli
    if dates.advice_starts_at is None or moment < dates.advice_starts_at:
        return BASELINE
    if dates.ends_at is None or moment < dates.ends_at:
        return ADVICE
    return ENDED
