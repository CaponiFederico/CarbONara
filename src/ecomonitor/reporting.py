from dataclasses import dataclass
from datetime import date

from ecomonitor.storage.db import LocalDatabase

#Wh di una ricarica completa di uno smartphone TODO segnaposto
WH_PER_PHONE_CHARGE = 13.0


def format_phone_charges(count: float) -> str:
    if count >= 10:
        return f"{count:.0f}"
    if count >= 1:
        return f"{count:.1f}"
    if count > 0 and count < 0.005:
        #sotto la risoluzione delle due cifre: "0.00" 
        return "< 0.01"
    return f"{count:.2f}"


@dataclass(frozen=True)
class DailyReport:
    day: date
    energy_wh: float
    operational_gco2: float
    active_hours: float

    @property
    def equivalent_phone_charges(self) -> float:
        return self.energy_wh / WH_PER_PHONE_CHARGE


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
