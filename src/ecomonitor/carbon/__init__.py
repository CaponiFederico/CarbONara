from typing import Any

from ecomonitor.carbon.battery_pool import AttributionModel, BatteryCarbonPool

__all__ = [
    "AttributionModel",
    "BatteryCarbonPool",
    "GridCarbonIntensityService",
]


def __getattr__(name: str) -> Any:
    #il costo lo paga solo chi lo usa, risparmio diversi ms
    if name == "GridCarbonIntensityService":
        from ecomonitor.carbon.grid_intensity import GridCarbonIntensityService

        return GridCarbonIntensityService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
