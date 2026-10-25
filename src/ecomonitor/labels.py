#Etichette delle provenienze


SOURCE_LABELS = {
    "grid_live": "consumo da rete elettrica",
    "battery_pool": "storico di ricarica della batteria",
    "battery_pool_partial": "storico di ricarica parziale",
    "battery_pool_fallback": "stima (nessuno storico di ricarica disponibile)",
}

GRID_SOURCE_LABELS = {
    "live": "dati in tempo reale",
    "local_hour_median": "stima dalla stessa fascia oraria dei giorni scorsi",
    "static_fallback": "media statica di ripiego",
}

GRID_FAILURE_LABELS = {
    "zone": "zona non servita dal server",
    "unavailable": "dati di rete non disponibili sul server",
    "unreachable": "server non raggiungibile",
}

#forma breve
SHORT_SOURCE_LABELS = {
    "grid_live": "da rete",
    "battery_pool": "storico batteria",
    "battery_pool_partial": "storico parziale",
    "battery_pool_fallback": "senza storico",
}


def short_source_label(source: str) -> str:
    return SHORT_SOURCE_LABELS.get(source, source)


def grid_source_label(source: str) -> str:
    return GRID_SOURCE_LABELS.get(source, source)


def grid_source_detail(source: str, failure: str | None = None) -> str:
    label = grid_source_label(source)
    if source == "live":
        return label
    return f"{label} — {grid_failure_label(failure) or 'nessun server configurato'}"


def grid_failure_label(failure: str | None) -> str | None:
    if failure is None:
        return None
    return GRID_FAILURE_LABELS.get(failure, failure)
