import json
import logging
import math
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import platformdirs

from ecomonitor.carbon.battery_pool import AttributionModel

#Configurazione in JSON nella cartella utente di sistema, quindi fuori dal
#repository: la chiave ElectricityMaps e' un segreto personale

logger = logging.getLogger(__name__)

APP_NAME = "ecomonitor"
API_KEY_ENV_VAR = "ECOMONITOR_EM_API_KEY"

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
    #la rete si muove su scala oraria, e il tier gratuito dell'API ha un limite

    attribution_model: str = "weighted_average"  # weighted_average | fifo

    charge_rate_percent_per_hour: float = 50.0
    #velocita' tipica di ricarica, per stimare quando dentro una finestra ad
    #app chiusa la ricarica e' davvero avvenuta

    electricitymaps_api_key: str | None = None

    tray_notice_shown: bool = False
    #l'avviso "continuo a misurare in background" si mostra una volta sola

    @property
    def api_key(self) -> str | None:
        #la variabile d'ambiente ha la precedenza sul file
        return os.environ.get(API_KEY_ENV_VAR) or self.electricitymaps_api_key


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
    if config.electricitymaps_api_key is not None:
        config.electricitymaps_api_key = str(config.electricitymaps_api_key).strip() or None
    return config


class ConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()
        self.config = self._load()

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

        #ignora le chiavi sconosciute invece di sollevare TypeError: un file
        #scritto da un'altra versione non deve impedire l'avvio
        known = {field.name for field in fields(AppConfig)}
        return _sanitised(
            AppConfig(**{key: value for key, value in data.items() if key in known})
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
            quarantined = None

        logger.warning(
            "Configurazione non leggibile (%s). Si riparte dai valori predefiniti; "
            "il file precedente %s",
            reason,
            f"e' stato rinominato in {quarantined.name}" if quarantined else
            "non e' stato possibile metterlo da parte",
        )
        return self._create_default()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.config), indent=2), encoding="utf-8")
