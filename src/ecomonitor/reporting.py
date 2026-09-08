from dataclasses import dataclass
from datetime import date

from ecomonitor.storage.db import LocalDatabase

#Wh di una ricarica completa di uno smartphone TODO segnaposto
WH_PER_PHONE_CHARGE = 13.0

#gCO2eq di una email breve 
#https://carbonliteracy.com/the-carbon-cost-of-an-email/
G_CO2_PER_EMAIL = 0.3


def format_energy(wh: float) -> str:
    #migliorare la leggibilita' dei numeri: 1.234,56 Wh e' piu' leggibile di 1234.56 Wh
    if abs(wh) >= 1000.0:
        return f"{wh / 1000.0:.2f} kWh"
    if 0 < wh < 0.05:
        return "< 0.1 Wh"
    return f"{wh:.1f} Wh"


def format_gco2(grams: float) -> str:
    if abs(grams) >= 1000.0:
        return f"{grams / 1000.0:.2f} kg"
    if 0 < grams < 0.5:
        return "< 1 g"
    return f"{grams:.0f} g"


def format_duration(seconds: float) -> str:
    if seconds < 90.0:
        return f"{seconds:.0f} s"
    if seconds < 5400.0:
        return f"{seconds / 60.0:.0f} min"
    return f"{seconds / 3600.0:.1f} h"


def format_email_count(count: float) -> str:
    if count >= 10:
        return f"{count:.0f}"
    if count >= 1:
        return f"{count:.1f}"
    if count > 0:
        return "< 1"
    return "0"


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

    @property
    def equivalent_emails(self) -> float:
        #dai grammi e non dall'energia
        return self.operational_gco2 / G_CO2_PER_EMAIL


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
