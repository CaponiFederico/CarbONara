from dataclasses import dataclass
from datetime import date

from ecomonitor.storage.db import LocalDatabase

#gCO2eq/km indicativi per un'auto media europea a benzina: segnaposto, da
#sostituire con una fonte citabile prima di usarlo in tesi
GCO2_PER_KM_CAR = 120.0


@dataclass(frozen=True)
class DailyReport:
    day: date
    energy_wh: float
    operational_gco2: float
    active_hours: float

    @property
    def equivalent_car_km(self) -> float:
        return self.operational_gco2 / GCO2_PER_KM_CAR


class ReportingEngine:
    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    def build_daily_report(self, day: date, active_hours: float) -> DailyReport:
        totals = self._db.sum_energy_events_for_day(day)

        self._db.upsert_daily_summary(
            day=day,
            energy_wh=totals["energy_wh"],
            operational_gco2=totals["operational_gco2"],
            active_hours=active_hours,
        )

        return DailyReport(
            day=day,
            energy_wh=totals["energy_wh"],
            operational_gco2=totals["operational_gco2"],
            active_hours=active_hours,
        )
