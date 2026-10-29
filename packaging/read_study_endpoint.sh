#!/bin/bash
#Indirizzo del server dello studio, scritto nel pacchetto come file
#study_endpoint accanto all'eseguibile
set -euo pipefail

PROJECT_ROOT="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
ENDPOINT_FILE="${PROJECT_ROOT}/.study_endpoint"

endpoint="${ECOMONITOR_STUDY_ENDPOINT:-}"
if [[ -z ${endpoint} && -f ${ENDPOINT_FILE} ]]; then
    endpoint="$(tr -d '[:space:]' <"${ENDPOINT_FILE}")"
fi

if [[ -z ${endpoint} ]]; then
    echo "ERRORE: nessun indirizzo del server dello studio" >&2
    echo "Metterlo in ${ENDPOINT_FILE} oppure in ECOMONITOR_STUDY_ENDPOINT." >&2
    exit 2
fi

#il token viaggia nell'intestazione
if [[ ${endpoint} != https://* ]]; then
    echo "ERRORE: l'indirizzo del server dello studio deve iniziare con https://" >&2
    exit 2
fi

printf '%s' "${endpoint%/}"
