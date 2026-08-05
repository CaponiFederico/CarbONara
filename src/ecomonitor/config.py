import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import platformdirs

#Configurazione in JSON nella cartella utente di sistema, quindi fuori dal
#repository: la chiave ElectricityMaps e' un segreto personale

APP_NAME = "ecomonitor"
API_KEY_ENV_VAR = "ECOMONITOR_EM_API_KEY"


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


class ConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()
        self.config = self._load()

    def _load(self) -> AppConfig:
        if not self.path.exists():
            #il file va creato davvero: e' l'unico posto in cui incollare la
            #chiave o correggere la zona
            self.config = AppConfig()
            try:
                self.save()
            except OSError:
                pass  # cartella non scrivibile: si parte con i default in memoria
            return self.config

        data = json.loads(self.path.read_text(encoding="utf-8"))

        #ignora le chiavi sconosciute invece di sollevare TypeError: un file
        #scritto da un'altra versione non deve impedire l'avvio
        known = {field.name for field in fields(AppConfig)}
        return AppConfig(**{key: value for key, value in data.items() if key in known})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.config), indent=2), encoding="utf-8")
