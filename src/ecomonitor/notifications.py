
#Decide quando una notifica di sistema ha senso e come deve essere formulata
#Non si occupa di invio o ritiro: e' solo logica 

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from ecomonitor.advisor import (
    AdviceSource,
    ChargeAdvice,
    ChargeRecommendation,
    ReferenceSource,
    _percent_phrase,
)
from ecomonitor.carbon.grid_intensity import LIVE_FAILURE_AUTH
from ecomonitor.storage.db import LIVE_SAMPLE_SOURCE
from ecomonitor.study_phase import StudyPhase

STARTUP_GRACE_SECONDS = 120.0

ADVICE_STABLE_SECONDS = 60.0
#quanto deve reggere un buon momento prima di essere notificato

MIN_ADVICE_SPACING_SECONDS = 2 * 3600.0
MAX_CHARGE_ADVICE_PER_DAY = 3
MAX_WORKLOAD_ADVICE_PER_DAY = 2

CHARGE_MAX_BATTERY_PERCENT = 80.0
#sopra, ricaricare non serve

BETTER_TIME_MIN_SAVING_PERCENT = 20.0
BETTER_TIME_MIN_BATTERY_PERCENT = 40.0
#sotto, chi ha appena collegato il portatile

ADVICE_LIFETIME_SECONDS = 3600.0

WORKLOAD_GOOD_RATIO = 0.85
CONTINUITY_GAP_SECONDS = 120.0


class NotificationKind(Enum):
    GOOD_TIME_TO_CHARGE = "good_time_to_charge"
    # portatile: la rete e' al minimo previsto

    BETTER_TIME_AHEAD = "better_time_ahead"
    # portatile appena collegato in un momento sporco

    GOOD_TIME_FOR_WORKLOAD = "good_time_for_workload"
    # desktop: la rete e' al minimo previsto e sotto la media

    MEASUREMENT_DEGRADED = "measurement_degraded"
    # sensore perso durante la sessione

    API_KEY_REJECTED = "api_key_rejected"
    STUDY_TOKEN_REJECTED = "study_token_rejected"

    STUDY_ADVICE_STARTED = "study_advice_started"
    # studio: inizia la fase 2, l'app mostra i consigli


PROBLEM_KINDS = frozenset(
    {
        NotificationKind.MEASUREMENT_DEGRADED,
        NotificationKind.API_KEY_REJECTED,
        NotificationKind.STUDY_TOKEN_REJECTED,
    }
)

OWN_SLOT_KINDS = PROBLEM_KINDS | {NotificationKind.STUDY_ADVICE_STARTED}


@dataclass(frozen=True)
class Notification:
    kind: NotificationKind
    title: str
    text: str
    expires_at: datetime | None = None

    @property
    def is_problem(self) -> bool:
        return self.kind in PROBLEM_KINDS


@dataclass(frozen=True)
class TickSnapshot:
    advice: ChargeAdvice
    is_plugged_in: bool
    battery_percent: float | None  # None su desktop
    measurement_is_native: bool
    grid_source: str
    grid_failure: str | None
    study_token_rejected: bool
    window_in_foreground: bool
    advice_enabled: bool 
    problems_enabled: bool


@dataclass(frozen=True)
class PolicyOutcome:
    show: Notification | None = None
    withdraw: tuple[NotificationKind, ...] = ()


def advice_notifications_allowed(user_enabled: bool, study_phase: StudyPhase | None) -> bool:
    #una notifica e' un intervento: in fase 1 dello studio non ce ne sono
    if study_phase is StudyPhase.BASELINE:
        return False
    return user_enabled


def study_advice_started_notification() -> Notification:
    #una volta sola, all'inizio della fase 2
    return Notification(
        NotificationKind.STUDY_ADVICE_STARTED,
        "EcoMonitor ora mostra i consigli",
        _lines("Ti dice quando la rete è più pulita", "Seconda settimana dello studio"),
    )


def snapshot_from_tick(
    result: dict,
    window_in_foreground: bool,
    advice_enabled: bool,
    problems_enabled: bool,
) -> TickSnapshot:
    power_state = result["power_state"]
    return TickSnapshot(
        advice=result["advice"],
        is_plugged_in=power_state.is_plugged_in,
        battery_percent=power_state.battery_percent,
        measurement_is_native=result["measurement_mode"].is_complete,
        grid_source=result["grid_intensity_source"],
        grid_failure=result["grid_intensity_failure"],
        study_token_rejected=bool(result.get("study_token_rejected", False)),
        window_in_foreground=window_in_foreground,
        advice_enabled=advice_enabled,
        problems_enabled=problems_enabled,
    )


def _clock_text(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def _horizon_text(advice: ChargeAdvice) -> str:
    hours = advice.reference_horizon_hours
    return f"delle prossime {hours:g} h" if hours else "delle prossime ore"


MAX_LINE_CHARS = 42

def _lines(first: str, second: str) -> str:
    return f"{first}\n{second}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


_PROBLEM_TEXTS = {
    NotificationKind.MEASUREMENT_DEGRADED: (
        "Misura passata a stima",
        _lines("Riavvia l'app per tornare a misurare", "Il sensore hardware non risponde più"),
    ),
    NotificationKind.API_KEY_REJECTED: (
        "Chiave ElectricityMaps rifiutata",
        _lines("Controlla la chiave nelle impostazioni", "Dati di rete in tempo reale spenti"),
    ),
    NotificationKind.STUDY_TOKEN_REJECTED: (
        "Token dello studio rifiutato",
        _lines("Controlla il token nelle impostazioni", "L'invio dei dati allo studio è fermo"),
    ),
}


class NotificationPolicy:
    def __init__(self, started_at: datetime) -> None:
        self._started_at = started_at
        self._last_seen_at: datetime | None = None
        self._previous_plugged: bool | None = None

        self._good_since: datetime | None = None
        self._good_announced = False
        self._live_good_kind: NotificationKind | None = None

        self._advice_day: date | None = None
        self._advice_count = 0
        self._last_advice_at: datetime | None = None

        self._seen_native = False
        self._announced_problems: set[NotificationKind] = set()

    def evaluate(self, snapshot: TickSnapshot, now: datetime) -> PolicyOutcome:
        continuous = (
            self._last_seen_at is not None
            and (now - self._last_seen_at).total_seconds() <= CONTINUITY_GAP_SECONDS
        )
        if not continuous:
            self._good_since = None
            self._previous_plugged = None
        just_plugged = self._previous_plugged is False and snapshot.is_plugged_in
        self._last_seen_at = now
        self._previous_plugged = snapshot.is_plugged_in

        in_grace = (now - self._started_at).total_seconds() < STARTUP_GRACE_SECONDS

        problem, resolved = self._problem(snapshot, in_grace)
        ended = self._track_good_time(snapshot, now)
        withdraw = resolved if ended is None else (*resolved, ended)
        if problem is not None:
            return PolicyOutcome(show=problem, withdraw=withdraw)

        advice = self._advice(snapshot, now, just_plugged, in_grace)
        if advice is not None and advice.kind is not NotificationKind.BETTER_TIME_AHEAD:
            self._live_good_kind = advice.kind
        return PolicyOutcome(show=advice, withdraw=withdraw)

    # problemi

    def _problem(
        self, snapshot: TickSnapshot, in_grace: bool
    ) -> tuple[Notification | None, tuple[NotificationKind, ...]]:
        #la notifica da mostrare, e i problemi annunciati che nel frattempo sono rientrati
        if snapshot.measurement_is_native:
            self._seen_native = True
        active = {
            NotificationKind.MEASUREMENT_DEGRADED: (
                self._seen_native and not snapshot.measurement_is_native
            ),
            NotificationKind.API_KEY_REJECTED: snapshot.grid_failure == LIVE_FAILURE_AUTH,
            NotificationKind.STUDY_TOKEN_REJECTED: snapshot.study_token_rejected,
        }
        resolved = tuple(
            kind
            for kind, is_active in active.items()
            if not is_active and kind in self._announced_problems
        )
        self._announced_problems.difference_update(resolved)

        if in_grace or not snapshot.problems_enabled:
            return None, resolved
        for kind, is_active in active.items():
            if is_active and kind not in self._announced_problems:
                self._announced_problems.add(kind)
                title, text = _PROBLEM_TEXTS[kind]
                return Notification(kind, title, text), resolved
        return None, resolved

    # consigli

    @staticmethod
    def _is_reliable(snapshot: TickSnapshot) -> bool:
        return (
            snapshot.advice.source is AdviceSource.FORECAST
            and snapshot.grid_source == LIVE_SAMPLE_SOURCE
        )

    def _is_good_time(self, snapshot: TickSnapshot) -> bool:
        advice = snapshot.advice
        if not self._is_reliable(snapshot):
            return False
        if advice.recommendation is not ChargeRecommendation.CHARGE_NOW:
            return False
        if snapshot.battery_percent is not None:
            #portatile: conta solo a batteria
            return (
                not snapshot.is_plugged_in
                and snapshot.battery_percent <= CHARGE_MAX_BATTERY_PERCENT
            )
        #desktop
        if (
            advice.reference_source is not ReferenceSource.FORECAST_MEAN
            or advice.current_intensity is None
            or advice.reference_intensity is None
            or advice.reference_intensity <= 0
        ):
            return False
        return advice.current_intensity <= WORKLOAD_GOOD_RATIO * advice.reference_intensity

    def _track_good_time(self, snapshot: TickSnapshot, now: datetime) -> NotificationKind | None:
        #la notifica di buon momento da ritirare, se era stata mostrata ed e' finito
        if self._is_good_time(snapshot):
            if self._good_since is None:
                self._good_since = now
            return None
        self._good_since = None
        self._good_announced = False
        ended, self._live_good_kind = self._live_good_kind, None
        return ended

    def _within_budget(self, snapshot: TickSnapshot, now: datetime) -> bool:
        today = now.astimezone().date()
        if today != self._advice_day:
            self._advice_day = today
            self._advice_count = 0
        limit = (
            MAX_WORKLOAD_ADVICE_PER_DAY
            if snapshot.battery_percent is None
            else MAX_CHARGE_ADVICE_PER_DAY
        )
        if self._advice_count >= limit:
            return False
        return (
            self._last_advice_at is None
            or (now - self._last_advice_at).total_seconds() >= MIN_ADVICE_SPACING_SECONDS
        )

    def _advice(
        self, snapshot: TickSnapshot, now: datetime, just_plugged: bool, in_grace: bool
    ) -> Notification | None:
        candidate = self._better_time_ahead(snapshot) if just_plugged else None
        is_good_time = candidate is None and (
            self._good_since is not None
            and not self._good_announced
            and (now - self._good_since).total_seconds() >= ADVICE_STABLE_SECONDS
        )
        if candidate is None and not is_good_time:
            return None

        if snapshot.window_in_foreground:
            #il consiglio e' gia' sullo schermo
            if is_good_time:
                self._good_announced = True
            return None
        if in_grace or not snapshot.advice_enabled or not self._within_budget(snapshot, now):
            return None

        if candidate is None:
            candidate = self._good_time(snapshot, now)
            self._good_announced = True
        self._advice_count += 1
        self._last_advice_at = now
        return candidate

    def _good_time(self, snapshot: TickSnapshot, now: datetime) -> Notification:
        advice = snapshot.advice
        grid_now = f"Rete a {advice.current_intensity:.0f} gCO2/kWh alle {_clock_text(now)}"
        expires_at = now + timedelta(seconds=ADVICE_LIFETIME_SECONDS)
        if snapshot.battery_percent is not None:
            return Notification(
                NotificationKind.GOOD_TIME_TO_CHARGE,
                "Buon momento per ricaricare",
                _lines(f"È il minimo {_horizon_text(advice)}", grid_now),
                expires_at,
            )
        #_is_good_time ha gia' verificato che i due valori ci sono
        assert advice.current_intensity is not None
        assert advice.reference_intensity is not None
        below = (1.0 - advice.current_intensity / advice.reference_intensity) * 100.0
        return Notification(
            NotificationKind.GOOD_TIME_FOR_WORKLOAD,
            "Buon momento per le attività pesanti",
            _lines(
                f"{_sentence(_percent_phrase(below))} sotto la media {_horizon_text(advice)}",
                grid_now,
            ),
            expires_at,
        )

    def _better_time_ahead(self, snapshot: TickSnapshot) -> Notification | None:
        advice = snapshot.advice
        if snapshot.battery_percent is None or not self._is_reliable(snapshot):
            return None
        if snapshot.battery_percent < BETTER_TIME_MIN_BATTERY_PERCENT:
            return None
        if (
            advice.recommendation is not ChargeRecommendation.WAIT
            or advice.best_time is None
            or advice.best_intensity is None
            or advice.current_intensity is None
            or advice.saving_percent is None
            or advice.saving_percent < BETTER_TIME_MIN_SAVING_PERCENT
        ):
            return None
        return Notification(
            NotificationKind.BETTER_TIME_AHEAD,
            "Più tardi la rete è più pulita",
            _lines(
                f"Alle {_clock_text(advice.best_time)} scende a {advice.best_intensity:.0f} "
                f"gCO2/kWh (-{advice.saving_percent:.0f}%)",
                f"Ora è a {advice.current_intensity:.0f}: se puoi, ricarica allora",
            ),
            #passata quell'ora il suggerimento non ha piu' senso
            advice.best_time,
        )
