#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"

key="${ECOMONITOR_EM_API_KEY:-}"
if [[ -z ${key} && -f ${PROJECT_ROOT}/.env ]]; then
    key="$(tr -d '[:space:]' <"${PROJECT_ROOT}/.env")"
fi

if [[ -z ${key} ]]; then
    echo "ERRORE: nessuna chiave ElectricityMaps" >&2
    echo "Metterla in ${PROJECT_ROOT}/.env oppure in ECOMONITOR_EM_API_KEY." >&2
    exit 2
fi

if [[ ${key} == *=* ]]; then
    echo "ERRORE: ${PROJECT_ROOT}/.env deve contenere solo la chiave, non NOME=valore." >&2
    exit 2
fi

printf '%s' "${key}"
