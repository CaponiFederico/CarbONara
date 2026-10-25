import json
import logging
import math
import os
import platform
import re
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import platformdirs

from ecomonitor.carbon.battery_pool import AttributionModel
from ecomonitor.study_phase import (
    INSTALL_ID_PATTERN,
    StudyDates,
    StudyPhase,
    current_phase,
    format_moment,
    parse_moment,
    recorded_phase,
)

#Configurazione in JSON nella cartella utente di sistema, quindi fuori dal
#repository: il token dello studio e' personale

logger = logging.getLogger(__name__)

APP_NAME = "ecomonitor"

PACKAGED_STUDY_ENDPOINT_FILE = "study_endpoint"

#campi che le versioni precedenti scrivevano e che non devono restare sul disco: la
#chiave ElectricityMaps ora sta solo sul server, e una copia nel config.json resterebbe
#li' in chiaro per sempre
REMOVED_FIELDS = ("electricitymaps_api_key",)

#server dello studio in locale, per collaudare l'invio da sorgente
LOCAL_STUDY_HOSTS = ("127.0.0.1", "localhost")

#lo stesso formato che il server accetta
STUDY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
#l'impronta SHA-256 del token, che non va scambiata per il token
TOKEN_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

ATTRIBUTION_MODELS: tuple[str, ...] = tuple(model.value for model in AttributionModel)


@dataclass
class AppConfig:
    em_zone: str = "IT-CNO"
    #zona di mercato elettrico, non il codice del paese: per l'Italia IT-NO,
    #IT-CNO (Pisa), IT-CSO, IT-SO, IT-SIC, IT-SAR

    country_code: str = "IT"
    #usato dal dataset statico di ripiego

    poll_interval_seconds: float = 5.0

    db_flush_interval_seconds: float = 60.0
    #ogni quanto scrivere una riga aggregata: la misura ha bisogno di 5 s, la
    #persistenza no

    grid_sample_interval_seconds: float = 300.0
    #la rete si muove su scala oraria, e il server la rilegge ogni 5 minuti

    attribution_model: str = "weighted_average"  # weighted_average | fifo

    charge_rate_percent_per_hour: float = 50.0
    #velocita' tipica di ricarica, per stimare quando dentro una finestra ad
    #app chiusa la ricarica e' davvero avvenuta

    tray_notice_shown: bool = False
    #l'avviso "continuo a misurare in background" si mostra una volta sola

    background_notice_shown: bool = False
    #lo stesso avviso, ma dove non c'e' area di notifica

    autostart_default_applied: bool = False
    #l'avvio automatico nasce attivo, ma una volta sola

    study_endpoint: str | None = None
    #indirizzo del server dello studio, che da' anche i dati di rete a tutte le app

    study_token: str | None = None
    #token personale del partecipante

    advice_notifications_enabled: bool = True
    #consigli sul momento migliore

    problem_notifications_enabled: bool = True
    #misura scesa a stima, token rifiutato

    study_starts_at: str | None = None
    study_advice_starts_at: str | None = None
    study_ends_at: str | None = None
    #calendario dello studio deciso dal server

    study_advice_announced: bool = False
    #l'inizio della fase 2 si annuncia una volta sola

    study_onboarding_done: bool = False
    #la schermata che chiede il codice dello studio al primo avvio

    install_id: str | None = None
    #UUID casuale dell'installazione, inviato allo studio

    @property
    def effective_study_endpoint(self) -> str | None:
        #quello del config.json vince su quello spedito
        return self.study_endpoint or packaged_study_endpoint()

    def study_phase(self, now: datetime) -> StudyPhase | None:
        return current_phase(
            bool(self.study_token),
            parse_moment(self.study_advice_starts_at),
            parse_moment(self.study_ends_at),
            now,
        )

    def recorded_study_phase(self, now: datetime) -> StudyPhase | None:
        return recorded_phase(self.study_phase(now), parse_moment(self.study_starts_at), now)

    def apply_study_dates(self, dates: StudyDates) -> None:
        self.study_starts_at = format_moment(dates.starts_at)
        self.study_advice_starts_at = format_moment(dates.advice_starts_at)
        self.study_ends_at = format_moment(dates.ends_at)

    def reset_study_calendar(self) -> None:
        #un token diverso e' un altro partecipante
        self.study_starts_at = None
        self.study_advice_starts_at = None
        self.study_ends_at = None
        self.study_advice_announced = False


def _packaged_text(name: str) -> str | None:
    if not getattr(sys, "frozen", False):
        return None
    executable_dir = Path(sys.executable).parent
    candidates = [executable_dir / name]
    if platform.system() == "Darwin":
        candidates.insert(0, executable_dir.parent / "Resources" / name)
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text
    return None

def packaged_study_endpoint() -> str | None:
    #passa dallo stesso controllo del config.json
    return _study_endpoint(_packaged_text(PACKAGED_STUDY_ENDPOINT_FILE))


def default_config_path() -> Path:
    return platformdirs.user_config_path(APP_NAME) / "config.json"


def _positive_number(value: object, default: float, field_name: str, maximum: float) -> float:
    #Un JSON valido puo' contenere valori senza senso
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "Configurazione: %s='%r' non e' un numero, uso il valore predefinito %g",
            field_name,
            value,
            default,
        )
        return default

    #math.isfinite scarta NaN E infinito
    if not math.isfinite(number) or not (0 < number <= maximum):
        logger.warning(
            "Configurazione: %s=%r fuori dall'intervallo ammesso (0, %g], "
            "uso il valore predefinito %g",
            field_name,
            value,
            maximum,
            default,
        )
        return default
    return number


def _non_empty_text(value: object, default: str, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        logger.warning(
            "Configurazione: %s vuoto, uso il valore predefinito '%s'", field_name, default
        )
        return default
    return text


def _sanitised(config: AppConfig) -> AppConfig:
    #Normalizza i valori appena letti dal file
    defaults = AppConfig()

    if config.attribution_model not in ATTRIBUTION_MODELS:
        #arrivava intatto fino a AttributionModel(...) nel costruttore
        #dell'Orchestrator: ValueError non gestita, finestra che non compare
        logger.warning(
            "Configurazione: modello di attribuzione '%s' sconosciuto, uso '%s' "
            "(validi: %s)",
            config.attribution_model,
            defaults.attribution_model,
            ", ".join(ATTRIBUTION_MODELS),
        )
        config.attribution_model = defaults.attribution_model

    config.em_zone = _non_empty_text(config.em_zone, defaults.em_zone, "em_zone")

    config.country_code = _non_empty_text(
        config.country_code, defaults.country_code, "country_code"
    ).upper()

    #valore finito ma assurdo
    for field_name, maximum in (
        #oltre un'ora fra due campioni non si misura piu' niente di utile
        ("poll_interval_seconds", 3600.0),
        #una giornata: oltre, una riga aggregata non e' piu' una riga
        ("db_flush_interval_seconds", 86400.0),
        ("grid_sample_interval_seconds", 86400.0),
        #100%/h e' gia' ricarica rapida; mille e' oltre qualunque hardware
        ("charge_rate_percent_per_hour", 1000.0),
    ):
        setattr(
            config,
            field_name,
            _positive_number(
                getattr(config, field_name), getattr(defaults, field_name), field_name, maximum
            ),
        )

    config.tray_notice_shown = bool(config.tray_notice_shown)
    config.background_notice_shown = bool(config.background_notice_shown)
    config.autostart_default_applied = bool(config.autostart_default_applied)
    config.advice_notifications_enabled = bool(config.advice_notifications_enabled)
    config.problem_notifications_enabled = bool(config.problem_notifications_enabled)
    config.study_token = _study_token(config.study_token)
    config.study_endpoint = _study_endpoint(config.study_endpoint)
    config.study_advice_announced = bool(config.study_advice_announced)
    config.study_onboarding_done = bool(config.study_onboarding_done)
    config.study_starts_at = format_moment(parse_moment(config.study_starts_at))
    config.study_advice_starts_at = format_moment(parse_moment(config.study_advice_starts_at))
    config.study_ends_at = format_moment(parse_moment(config.study_ends_at))
    install_id = _optional_text(config.install_id)
    config.install_id = (
        install_id if install_id and INSTALL_ID_PATTERN.fullmatch(install_id) else None
    )
    return config


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _study_token(value: object) -> str | None:
    #un token malformato verrebbe rifiutato a ogni invio
    text = _optional_text(value)
    if text is None or STUDY_TOKEN_PATTERN.fullmatch(text):
        return text
    logger.warning("Configurazione: study_token non valido, l'invio allo studio resta spento")
    return None


def _study_endpoint(value: object) -> str | None:
    #il token viaggia nell'intestazione
    text = _optional_text(value)
    if text is None:
        return None
    parts = urlsplit(text)
    if parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in LOCAL_STUDY_HOSTS
    ):
        return text.rstrip("/")
    logger.warning(
        "Configurazione: study_endpoint '%s' non e' https, l'invio allo studio resta spento",
        text,
    )
    return None


class ConfigManager:
    _saved: dict[str, object] | None = None

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()
        self.config = self._load()
        self._saved = asdict(self.config)

    def _load(self) -> AppConfig:
        if not self.path.exists():
            return self._create_default()

        try:
            #utf-8-sig, con utf-8 puro il BOM fa
            #fallire json.loads e l'applicazione non parte
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._recover_from_invalid(exc)

        if not isinstance(data, dict):
            return self._recover_from_invalid("il contenuto non e' un oggetto JSON")

        if any(name in data for name in REMOVED_FIELDS):
            self._purge_removed_fields(data)

        #ignora le chiavi sconosciute invece di sollevare TypeError: un file
        #scritto da un'altra versione non deve impedire l'avvio
        known = {field.name for field in fields(AppConfig)}
        return _sanitised(
            AppConfig(**{key: value for key, value in data.items() if key in known})
        )

    def _purge_removed_fields(self, data: dict) -> None:
        try:
            self._write_atomically(
                {key: value for key, value in data.items() if key not in REMOVED_FIELDS}
            )
        except OSError as exc:
            #save() non li riscrive comunque: _read_raw() li lascia fuori
            logger.warning("Configurazione: campi vecchi non tolti dal file (%s)", exc)
            return
        logger.info(
            "Configurazione: tolta la chiave ElectricityMaps salvata, i dati di rete "
            "arrivano dal server di EcoMonitor"
        )

    def _create_default(self) -> AppConfig:
        #il file va creato
        self.config = AppConfig()
        try:
            self.save()
        except OSError:
            pass  # cartella non scrivibile: si parte con i default in memoria
        return self.config

    def _recover_from_invalid(self, reason: object) -> AppConfig:
        #Il file esiste ma non e' leggibile
        quarantined = self.path.with_name(self.path.name + ".invalid")
        try:
            self.path.replace(quarantined)
        except OSError:
            moved = False
        else:
            moved = True

        logger.warning(
            "Configurazione non leggibile (%s). Si riparte dai valori predefiniti; "
            "il file precedente %s",
            reason,
            f"e' stato rinominato in {quarantined.name}" if moved else
            "non e' stato possibile metterlo da parte",
        )
        return self._create_default()

    def save(self) -> None:
        current = asdict(self.config)
        data: dict[str, object] = current
        if self._saved is not None:
            on_disk = self._read_raw()
            if on_disk is not None:
                changed = {
                    name: value
                    for name, value in current.items()
                    if value != self._saved.get(name)
                }
                data = {**on_disk, **changed}
        self._write_atomically(data)
        self._saved = current

    def _read_raw(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return {key: value for key, value in data.items() if key not in REMOVED_FIELDS}

    def _write_atomically(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
