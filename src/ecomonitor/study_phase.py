import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

logger = logging.getLogger(__name__)


class StudyPhase(Enum):
    BASELINE = "baseline"
    ADVICE = "advice"
    ENDED = "ended"


#UUID versione 4, come lo scrive uuid.uuid4() e come lo accetta il server
INSTALL_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
MOMENT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class StudyDates:
    #calendario deciso dal server con la verifica del codice
    advice_starts_at: datetime | None
    ends_at: datetime | None
    #la prima ora dello studio
    starts_at: datetime | None = None


class StudyStartFailed(Exception):
    #la verifica del codice non e' riuscita
    def __init__(self, message: str, rejected: bool = False) -> None:
        super().__init__(message)
        #il server non conosce il codice
        self.rejected = rejected


def new_install_id() -> str:
    #casuale: distingue due computer della stessa persona, non dice chi e'
    return str(uuid.uuid4())


def parse_moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def format_moment(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).strftime(MOMENT_FORMAT)


def current_phase(
    has_token: bool,
    advice_starts_at: datetime | None,
    ends_at: datetime | None,
    now: datetime,
) -> StudyPhase | None:
    #None: nessuno studio, l'app e' quella normale
    if not has_token:
        return None
    if advice_starts_at is None or now < advice_starts_at:
        return StudyPhase.BASELINE
    if ends_at is None or now < ends_at:
        return StudyPhase.ADVICE
    return StudyPhase.ENDED


def shows_dashboard(phase: StudyPhase | None) -> bool:
    #solo la fase 1 nasconde consumi e consigli
    return phase is not StudyPhase.BASELINE


def recorded_phase(
    phase: StudyPhase | None, starts_at: datetime | None, now: datetime
) -> StudyPhase | None:
    #la fase con cui si registrano le righe
    if starts_at is not None and now < starts_at:
        return None
    return phase


def hour_phase_label(phase: StudyPhase | None) -> str | None:
    if phase is None or phase is StudyPhase.ENDED:
        return None
    return phase.value
