#!/bin/bash
# Provenienza del bundle prodotto da PyInstaller, condivisa fra i tre script

os_release_field() {
    local field="$1"
    [[ -r /etc/os-release ]] || return 0
    (. /etc/os-release && printf '%s' "${!field:-}") || true
}

#famiglia della macchina su cui gira questo script
current_distro_family() {
    local id id_like
    id="$(os_release_field ID)"
    id_like="$(os_release_field ID_LIKE)"
    case " ${id} ${id_like} " in
    *" debian "* | *" ubuntu "*) echo debian ;;
    *" fedora "* | *" rhel "*) echo fedora ;;
    *) echo sconosciuta ;;
    esac
}

#scritto da build_linux.sh solo a build riuscita: se PyInstaller fallisce il file
#non c'e', e un bundle vecchio rimasto nella cartella non viene impacchettato per sbaglio
write_bundle_origin() {
    local origin_file="$1"
    {
        echo "family=$(current_distro_family)"
        echo "distro=$(os_release_field PRETTY_NAME)"
        echo "python=$("${2}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        echo "built=$(date -Is)"
    } >"${origin_file}"
}

#usato da build_deb.sh e build_rpm.sh prima di copiare il bundle nello stage
require_bundle_origin() {
    local origin_file="$1" expected="$2" rebuild_hint="$3" found
    if [[ ! -f ${origin_file} ]]; then
        echo "ERRORE: manca ${origin_file}: non si sa quale macchina" >&2
        echo "        ha costruito il bundle in dist/CarbONara." >&2
        echo "Un bundle della distribuzione sbagliata produce un pacchetto che si" >&2
        echo "installa e non parte. Ricostruirlo dove va impacchettato:" >&2
        echo "  ${rebuild_hint}" >&2
        return 2
    fi
    found="$(sed -n 's/^family=//p' "${origin_file}")"
    if [[ ${found} != "${expected}" ]]; then
        echo "ERRORE: il bundle in dist/CarbONara l'ha costruito una macchina" >&2
        echo "        '${found}', ma questo pacchetto e' per '${expected}'." >&2
        echo "Impacchettarlo lo stesso darebbe un pacchetto che si installa e non parte." >&2
        echo "  ${rebuild_hint}" >&2
        return 2
    fi
}
