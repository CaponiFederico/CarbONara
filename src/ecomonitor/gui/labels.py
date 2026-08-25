#Etichette
#in un modulo perche' le usano sia la finestra principale che di storico

SOURCE_LABELS = {
    "grid_live": "consumo da rete elettrica",
    "battery_pool": "storico di ricarica della batteria",
    "battery_pool_partial": "storico di ricarica parziale",
    "battery_pool_fallback": "stima (nessuno storico di ricarica disponibile)",
}

GRID_SOURCE_LABELS = {
    "live": "dati in tempo reale",
    "static_fallback": "media statica di ripiego, nessuna chiave API",
}

#forma breve
SHORT_SOURCE_LABELS = {
    "grid_live": "da rete",
    "battery_pool": "storico batteria",
    "battery_pool_partial": "storico parziale",
    "battery_pool_fallback": "senza storico",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def short_source_label(source: str) -> str:
    return SHORT_SOURCE_LABELS.get(source, source)


def grid_source_label(source: str) -> str:
    return GRID_SOURCE_LABELS.get(source, source)
