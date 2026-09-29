import argparse
import ctypes
import os
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

#CLI di verifica (python -m ecomonitor.diagnostics <comando>)

OK = "[ OK ]"
WARN = "[WARN]"
FAIL = "[FAIL]"
INFO = "[INFO]"

_failures = 0
_warnings = 0


def _line(marker: str, text: str) -> None:
    global _failures, _warnings
    if marker == FAIL:
        _failures += 1
    elif marker == WARN:
        _warnings += 1
    print(f"{marker} {text}")


def _title(text: str) -> None:
    print(f"\n=== {text}")


def _has_elevated_privileges() -> bool | None:
    #None se non determinabile su questa piattaforma
    if platform.system() == "Windows":
        try:
            #getattr e non ctypes.windll diretto
            windll = getattr(ctypes, "windll", None)
            if windll is None:
                return None
            return bool(windll.shell32.IsUserAnAdmin())
        except Exception:
            return None
    #getattr e non os.geteuid diretto: su Windows quella funzione non esiste
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None:
        return geteuid() == 0
    return None


# -- env: che macchina e' questa, e quale scenario copre --------------------


def cmd_env(args: argparse.Namespace) -> None:
    from ecomonitor.config import API_KEY_ENV_VAR, ConfigManager, default_config_path
    from ecomonitor.power_context import read_power_source_state
    from ecomonitor.sensors.factory import get_backend

    _title("Ambiente")
    print(f"{INFO} sistema      : {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"{INFO} processore   : {platform.processor() or 'n/d'}")
    print(f"{INFO} python       : {sys.version.split()[0]} ({sys.executable})")

    elevated = _has_elevated_privileges()
    if platform.system() == "Darwin":
        from ecomonitor.sensors.macos_powermetrics import permissions_command

        print(f"{INFO} privilegi elevati: non servono - conta la regola sudoers")
        print(f"{INFO} se la misura ripiega: {permissions_command()}")
    elif elevated is True:
        _line(OK, "privilegi elevati: si (amministratore/root)")
    elif elevated is False:
        _line(
            WARN,
            "privilegi elevati: no - i sensori hardware reali probabilmente non "
            "saranno leggibili e si usera' la stima TDP",
        )
    else:
        print(f"{INFO} privilegi elevati: non determinabile")

    if platform.system() == "Windows":
        #da LibreHardwareMonitor 0.9.5 la CPU si legge solo col driver PawnIO
        from ecomonitor.sensors.windows_lhm import pawnio_installed

        pawnio = pawnio_installed()
        if pawnio is True:
            _line(OK, "driver PawnIO: installato")
        elif pawnio is False:
            _line(
                WARN,
                "driver PawnIO: assente - la CPU restera' stimata anche da "
                "amministratore (installarlo da pawnio.eu o dall'installer)",
            )
        else:
            print(f"{INFO} driver PawnIO: non determinabile")

    power = read_power_source_state()
    if power.battery_percent is None:
        _line(INFO, "batteria: assente -> scenario DESKTOP (attribuzione sempre da rete)")
    else:
        _line(
            INFO,
            f"batteria: presente, {power.battery_percent:.0f}% "
            f"({'collegato' if power.is_plugged_in else 'a batteria'}) -> scenario LAPTOP",
        )

    backend = get_backend()
    #una lettura sola non basta: i contatori e cpu_percent hanno bisogno di un
    #campione precedente, la prima lettura vale sempre 0
    backend.read()
    time.sleep(1.0)
    reading = backend.read()
    name = type(backend).__name__
    if reading.is_estimated:
        _line(WARN, f"backend sensori: {name} in modalita' STIMA (ripiego TDP)")
    else:
        _line(OK, f"backend sensori: {name}, lettura hardware reale")

    domains = []
    if reading.package_w:
        domains.append("CPU")
    if reading.gpu_integrated_w is not None:
        domains.append("GPU integrata")
    if reading.gpu_dedicated_w is not None:
        domains.append("GPU dedicata")
    if reading.dram_w is not None:
        domains.append("DRAM")
    print(f"{INFO} domini letti : {', '.join(domains) if domains else 'nessuno'}")

    config = ConfigManager().config
    print(f"{INFO} config       : {default_config_path()}")
    print(f"{INFO} zona / paese : {config.em_zone} / {config.country_code}")
    print(f"{INFO} modello attr.: {config.attribution_model}")
    source = (
        f"variabile d'ambiente {API_KEY_ENV_VAR}"
        if os.environ.get(API_KEY_ENV_VAR)
        else "file di configurazione"
    )
    if config.api_key:
        _line(OK, f"chiave ElectricityMaps: presente (da {source})")
    else:
        _line(WARN, "chiave ElectricityMaps: assente - si usera' il dataset statico")


# -- sensors: i domini di potenza, campionati piu' volte --------------------


def cmd_sensors(args: argparse.Namespace) -> None:
    from ecomonitor.sensors.factory import get_backend

    interval = max(1.0, args.interval)
    samples = max(2, args.samples)

    _title(f"Sensori di potenza ({samples} campioni a {interval:.0f}s di distanza)")
    print(
        f"{INFO} campionamento dati da sensori hardware reali (o stima TDP se non leggibili) "
    )

    backend = get_backend()
    backend.read()  # warm-up

    print(f"\n{'#':>3}  {'CPU':>9}  {'GPU int':>9}  {'GPU ded':>9}  {'DRAM':>9}  {'totale':>9}")
    readings = []
    for index in range(samples):
        time.sleep(interval)
        reading = backend.read()
        readings.append(reading)

        def fmt(value: float | None) -> str:
            return f"{value:.2f} W" if value is not None else "     n/d"

        print(
            f"{index + 1:>3}  {reading.package_w:>7.2f} W  {fmt(reading.gpu_integrated_w):>9}  "
            f"{fmt(reading.gpu_dedicated_w):>9}  {fmt(reading.dram_w):>9}  {reading.total_w:>7.2f} W"
        )

    last = readings[-1]
    print()
    if last.is_estimated:
        _line(WARN, "letture STIMATE (ripiego TDP): servono privilegi elevati / driver")
    else:
        _line(OK, "letture da sensore hardware reale (is_estimated=False)")

    if all(r.package_w > 0 for r in readings):
        _line(OK, f"CPU: valori sempre positivi (ultimo {last.package_w:.2f} W)")
    else:
        _line(FAIL, "CPU: almeno un campione a 0 W - sensore non valido o permessi mancanti")

    if last.gpu_integrated_w is not None:
        _line(OK, f"GPU integrata letta ({last.gpu_integrated_w:.2f} W)")
    else:
        #sulla grafica AMD integrata i sensori Power vengono scartati
        _line(
            INFO,
            "GPU integrata non letta: assente, non esposta, o scartata perche' non "
            "attendibile (in tal caso il suo consumo resta contato dentro la CPU)",
        )

    if last.gpu_dedicated_w is not None:
        _line(OK, f"GPU dedicata letta ({last.gpu_dedicated_w:.2f} W)")
    else:
        _line(
            INFO,
            "GPU dedicata non letta: assente, oppure spenta in questo momento ",
        )

    if last.gpu_integrated_w is not None and last.gpu_dedicated_w is not None:
        _line(OK, "integrata e dedicata riportate SEPARATAMENTE, non sommate")

    if last.dram_w is not None:
        _line(
            WARN,
            f"DRAM riportata ({last.dram_w:.2f} W): atteso 'n/d' su CPU client - "
            "verificare che non sia un dominio interpretato male",
        )
    else:
        _line(OK, "DRAM non disponibile, come atteso su CPU client")

    if platform.system() == "Linux":
        _title("Dettaglio GPU dedicata su Linux (letture diverse dai domini RAPL)")
        from ecomonitor.sensors.linux_gpu import (
            IntelArcGpuReader,
            read_amd_gpu_power_w,
            read_nvidia_gpu_power_w,
        )

        nvidia = read_nvidia_gpu_power_w()
        print(
            f"{INFO} NVIDIA (nvidia-smi) : "
            f"{f'{nvidia:.2f} W' if nvidia is not None else 'non rilevata'}"
        )
        amd = read_amd_gpu_power_w()
        print(
            f"{INFO} AMD (hwmon amdgpu)  : {f'{amd:.2f} W' if amd is not None else 'non rilevata'}"
        )
        arc = IntelArcGpuReader()
        arc.read_power_w()  # warm-up: il contatore Intel richiede un delta
        time.sleep(interval)
        intel = arc.read_power_w()
        print(
            f"{INFO} Intel Arc (hwmon)   : "
            f"{f'{intel:.2f} W' if intel is not None else 'non rilevata'}"
        )


# -- grid: connettivita' e dati ElectricityMaps -----------------------------


def cmd_grid(args: argparse.Namespace) -> None:
    from ecomonitor.advisor import AdviceTarget, CarbonAwareAdvisor, RollingAverage
    from ecomonitor.carbon.grid_intensity import (
        HOUR_FALLBACK_DAYS,
        HOUR_MEDIAN_SOURCE,
        LIVE_FAILURE_AUTH,
        STATIC_FALLBACK_G_PER_KWH,
        GridCarbonIntensityService,
    )
    from ecomonitor.config import ConfigManager
    from ecomonitor.power_context import read_power_source_state
    from ecomonitor.storage.db import default_database_path

    config = ConfigManager().config
    service = GridCarbonIntensityService(
        zone=config.em_zone, api_key=config.api_key, country_code=config.country_code
    )

    _title(f"Intensita' di rete - zona {service.zone} (paese {service.country_code})")

    if not service.has_live_provider:
        _line(WARN, "nessuna chiave API: verra' usato solo il dataset statico")

    intensity = service.get_current_intensity()
    static = STATIC_FALLBACK_G_PER_KWH.get(
        service.country_code, STATIC_FALLBACK_G_PER_KWH["DEFAULT"]
    )
    fallback_label = (
        f"mediana della stessa fascia oraria negli ultimi {HOUR_FALLBACK_DAYS} giorni"
        if service.last_value_source == HOUR_MEDIAN_SOURCE
        else "media statica del paese"
    )
    if service.last_value_source == "live":
        _line(OK, f"valore live: {intensity:.0f} gCO2/kWh (media statica del paese: {static:.0f})")
    elif service.last_live_failure == LIVE_FAILURE_AUTH:
        #la chiave va sostituita
        _line(
            FAIL,
            "ElectricityMaps ha rifiutato la chiave API (HTTP 401 o 403): la chiave "
            "e' assente dal proprio account, scaduta o senza accesso a questa zona "
            f"({service.zone}). Valore di ripiego {intensity:.0f} gCO2/kWh dalla "
            f"{fallback_label}",
        )
    elif service.last_live_failure is not None:
        _line(
            WARN,
            f"chiave presente ma ElectricityMaps non risponde: valore di ripiego "
            f"{intensity:.0f} gCO2/kWh dalla {fallback_label}, controllare i log",
        )
    elif service.last_value_source == HOUR_MEDIAN_SOURCE:
        _line(
            INFO,
            f"valore ricostruito: {intensity:.0f} gCO2/kWh, mediana della stessa "
            f"fascia oraria negli ultimi {HOUR_FALLBACK_DAYS} giorni "
            f"(media statica del paese: {static:.0f})",
        )
    else:
        _line(INFO, f"valore statico: {intensity:.0f} gCO2/kWh")

    forecast = service.get_forecast()
    #Stessa finestra che usa l'advisor
    horizon_hours = CarbonAwareAdvisor().horizon_hours
    moment = datetime.now(UTC)
    horizon_end = moment + timedelta(hours=horizon_hours)
    upcoming = [p for p in forecast if moment < p.timestamp <= horizon_end]

    if forecast:
        _line(OK, f"previsione: {len(forecast)} punti orari")
        if upcoming:
            best = min(upcoming, key=lambda point: point.g_per_kwh)
            worst = max(upcoming, key=lambda point: point.g_per_kwh)
            print(
                f"{INFO} prossime {horizon_hours:g}h ({len(upcoming)} punti): "
                f"minimo {best.g_per_kwh:.0f} alle "
                f"{best.timestamp.astimezone():%H:%M}, massimo {worst.g_per_kwh:.0f} alle "
                f"{worst.timestamp.astimezone():%H:%M}"
            )
        else:
            _line(
                WARN,
                "previsione ricevuta ma senza punti entro l'orizzonte dell'advisor: "
                "verificare l'orologio di sistema",
            )
    else:
        _line(WARN, "previsione non disponibile: l'advisor usera' la media mobile locale")

    now = datetime.now(UTC)
    mean = service.get_mean_intensity_between(now - timedelta(hours=8), now)
    if mean is not None:
        _line(OK, f"storico ultime 8h: media {mean:.0f} gCO2/kWh (per le ricariche ad app chiusa)")
    else:
        _line(
            WARN,
            "storico non disponibile: le ricariche avvenute ad app chiusa useranno "
            "la media nazionale",
        )

    #stessa media mobile che usa l'app: senza, la diagnostica stamperebbe un
    #consiglio diverso da quello mostrato nella finestra sullo stesso istante
    rolling = RollingAverage()
    db_path = default_database_path()
    if db_path.exists():
        from ecomonitor.orchestrator import load_rolling_average
        from ecomonitor.storage.db import LocalDatabase

        rolling = load_rolling_average(LocalDatabase(db_path), service.zone)

    if rolling.average is not None:
        _line(
            OK,
            f"media recente della zona: {rolling.average:.0f} gCO2/kWh "
            f"({rolling.sample_count} campioni)",
        )
    else:
        _line(
            WARN,
            f"media recente non disponibile: {rolling.sample_count} campioni salvati, "
            f"ne servono almeno {rolling.min_samples}",
        )

    reference = [point.g_per_kwh for point in upcoming if point.g_per_kwh > 0]
    if reference:
        #e' il riferimento che l'advisor preferisce
        forecast_average = sum(reference) / len(reference)
        _line(
            OK,
            f"media prevista prossime {horizon_hours:g}h: {forecast_average:.0f} gCO2/kWh "
            "(riferimento preferito)",
        )

    #stesso stato di alimentazione che legge l'orchestratore: senza, la
    #diagnostica parlerebbe di ricarica anche a un desktop
    power_state = read_power_source_state()
    if power_state.battery_percent is None:
        power_label = "da rete elettrica, senza batteria"
    elif power_state.is_plugged_in:
        power_label = f"da rete elettrica, batteria al {power_state.battery_percent:.0f}%"
    else:
        power_label = f"a batteria, carica al {power_state.battery_percent:.0f}%"
    target = AdviceTarget.WORKLOAD if power_state.is_plugged_in else AdviceTarget.CHARGE
    advice = CarbonAwareAdvisor().advise(
        current_intensity=intensity,
        forecast=forecast,
        reference_intensity=rolling.average,
        battery_percent=power_state.battery_percent,
        target=target,
    )
    subject = "ricarica" if target is AdviceTarget.CHARGE else "attività pesanti"
    print(f"{INFO} alimentazione: {power_label}")
    print(f"{INFO} consiglio ora ({subject}): {advice.message()} (fonte: {advice.source.value})")


# -- pool: attribuzione e persistenza, senza hardware -----------------------


def cmd_pool(args: argparse.Namespace) -> None:
    from ecomonitor.carbon.attribution import (
        AttributionSource,
        CarbonAttributionService,
        gco2_from_wh,
    )
    from ecomonitor.carbon.battery_pool import AttributionModel, BatteryCarbonPool
    from ecomonitor.storage.db import LocalDatabase

    _title("Attribuzione del carbonio - scenario di riferimento della tesi")
    print(
        f"{INFO} 30% della carica a 500 gCO2/kWh, 40% a 150, 30% a 300;\n"
        f"       poi 10 Wh di CPU+GPU consumati scollegati."
    )

    results = {}
    for model in AttributionModel:
        pool = BatteryCarbonPool(model=model)
        for percent, grid in ((30.0, 500.0), (40.0, 150.0), (30.0, 300.0)):
            pool.charge(percent, grid)
        intensity = pool.discharge(5.0)
        results[model] = (intensity, gco2_from_wh(10.0, intensity))

    weighted_intensity, weighted_gco2 = results[AttributionModel.WEIGHTED_AVERAGE]
    fifo_intensity, fifo_gco2 = results[AttributionModel.FIFO]
    print(f"{INFO} media ponderata: {weighted_intensity:.0f} gCO2/kWh -> {weighted_gco2:.2f} g")
    print(f"{INFO} FIFO           : {fifo_intensity:.0f} gCO2/kWh -> {fifo_gco2:.2f} g")

    if abs(weighted_gco2 - 3.0) < 1e-9 and abs(fifo_gco2 - 5.0) < 1e-9:
        _line(OK, "entrambi i modelli danno i valori attesi (3.00 g e 5.00 g)")
    else:
        _line(FAIL, "valori diversi da quelli attesi: la logica del pool e' cambiata")

    _title("Attribuzione secondo lo stato di alimentazione")
    service = CarbonAttributionService(BatteryCarbonPool())
    service.pool.charge(50.0, 100.0)  # caricato quando la rete era pulita

    plugged = service.attribute(
        delta_wh=10.0,
        is_plugged_in=True,
        battery_percent=50.0,
        previous_battery_percent=50.0,
        grid_intensity_g_per_kwh=600.0,
    )
    unplugged = service.attribute(
        delta_wh=10.0,
        is_plugged_in=False,
        battery_percent=48.0,
        previous_battery_percent=50.0,
        grid_intensity_g_per_kwh=600.0,
    )

    print(
        f"{INFO} collegato  : {plugged.intensity_g_per_kwh:.0f} gCO2/kWh "
        f"via {plugged.source.value}"
    )
    print(
        f"{INFO} a batteria : {unplugged.intensity_g_per_kwh:.0f} gCO2/kWh "
        f"via {unplugged.source.value}"
    )

    if (
        plugged.source is AttributionSource.GRID_LIVE
        and plugged.intensity_g_per_kwh == 600.0
        and unplugged.source is AttributionSource.BATTERY_POOL
        and unplugged.intensity_g_per_kwh == 100.0
    ):
        _line(OK, "a batteria eredita il mix della ricarica, non quello attuale")
    else:
        _line(FAIL, "l'attribuzione non distingue correttamente rete e batteria")

    empty = CarbonAttributionService(BatteryCarbonPool()).attribute(
        delta_wh=10.0,
        is_plugged_in=False,
        battery_percent=70.0,
        previous_battery_percent=72.0,
        grid_intensity_g_per_kwh=350.0,
    )
    if empty.source is AttributionSource.BATTERY_POOL_FALLBACK and empty.gco2 > 0:
        _line(OK, "senza storico di ricarica ripiega su una stima, non su 0 gCO2")
    else:
        _line(FAIL, "senza storico di ricarica il consumo a batteria risulta gratuito")

    _title("Persistenza su database")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "pool_check.db"
        pool = BatteryCarbonPool(model=AttributionModel.FIFO)
        pool.charge(40.0, 120.0)
        pool.charge(20.0, 480.0)
        LocalDatabase(db_path).save_pool_state(pool, 60.0, datetime.now(UTC))

        restored, restored_percent, seen = LocalDatabase(db_path).load_pool_state()
        if (
            restored is not None
            and restored.model is AttributionModel.FIFO
            and abs(restored.stored_percent - 60.0) < 1e-9
            and restored.discharge(10.0) == 120.0
            and restored_percent == 60.0
            and seen is not None
        ):
            _line(OK, "stato del pool salvato e ricaricato identico (sopravvive al riavvio)")
        else:
            _line(FAIL, "lo stato del pool non sopravvive a un riavvio dell'applicazione")


# -- battery: osserva i Delta% reali, l'ingresso del pool -------------------


def cmd_battery(args: argparse.Namespace) -> None:
    import psutil

    _title(f"Osservazione della batteria per {args.minutes:g} minuti")

    previous = psutil.sensors_battery()
    if previous is None:
        _line(
            INFO,
            "nessuna batteria: su questo dispositivo il pool non entra mai in gioco "
            "e l'attribuzione e' sempre da rete. Controllo non applicabile.",
        )
        return

    print(
        f"{INFO} Collega e scollega l'alimentatore durante l'osservazione: serve a\n"
        f"       verificare che i Delta% - l'unico ingresso del pool - vengano visti."
    )

    deadline = time.monotonic() + args.minutes * 60
    charged = discharged = 0.0
    transitions = 0
    print(
        f"\n{INFO} stato iniziale: {previous.percent:.0f}% "
        f"({'collegato' if previous.power_plugged else 'a batteria'})"
    )

    while time.monotonic() < deadline:
        time.sleep(5)
        current = psutil.sensors_battery()
        if current is None:
            continue

        delta = current.percent - previous.percent
        if abs(delta) > 1e-9:
            if delta > 0:
                charged += delta
            else:
                discharged += -delta
            print(
                f"{INFO} {datetime.now():%H:%M:%S}  {previous.percent:.0f}% -> "
                f"{current.percent:.0f}% ({delta:+.1f} punti, "
                f"{'collegato' if current.power_plugged else 'a batteria'})"
            )
        if current.power_plugged != previous.power_plugged:
            transitions += 1
            print(
                f"{INFO} {datetime.now():%H:%M:%S}  alimentazione -> "
                f"{'collegato' if current.power_plugged else 'a batteria'}"
            )
        previous = current

    print()
    print(f"{INFO} totale osservato: +{charged:.1f} punti caricati, -{discharged:.1f} scaricati")
    if charged > 0 or discharged > 0:
        _line(OK, "variazioni di carica rilevate: il pool riceve i suoi pesi")
    else:
        _line(
            WARN,
            "nessuna variazione: la percentuale si muove lentamente, riprovare con "
            "--minutes maggiore o sotto carico",
        )
    if transitions > 0:
        _line(OK, f"{transitions} cambi di stato di alimentazione rilevati")
    else:
        _line(WARN, "nessun collega/scollega rilevato: passaggio rete<->batteria non verificato")


# -- live: l'orchestratore vero, su un database temporaneo ------------------


def cmd_live(args: argparse.Namespace) -> None:
    from ecomonitor.orchestrator import Orchestrator
    from ecomonitor.storage.db import LocalDatabase

    db_path = (
        Path(args.db)
        if args.db
        else Path(tempfile.mkdtemp(prefix="ecomonitor-verifica-")) / "live.db"
    )

    _title(f"Esecuzione reale per {args.minutes:g} minuti (database: {db_path})")
    print(f"{INFO} il database dell'applicazione NON viene toccato.")

    orchestrator = Orchestrator(db=LocalDatabase(db_path))
    deadline = time.monotonic() + args.minutes * 60
    sources: dict[str, int] = {}
    ticks = 0
    estimated_ticks = 0

    while time.monotonic() < deadline:
        result = orchestrator.tick()
        ticks += 1
        attribution = result["attribution"]
        sources[attribution.source.value] = sources.get(attribution.source.value, 0) + 1
        if result["reading"].is_estimated:
            estimated_ticks += 1

        state = result["power_state"]
        print(
            f"{INFO} {datetime.now():%H:%M:%S}  {result['reading'].total_w:6.2f} W  "
            f"{result['delta_wh']:7.4f} Wh  {result['gco2']:8.4f} gCO2  "
            f"{attribution.intensity_g_per_kwh:4.0f} gCO2/kWh via {attribution.source.value}  "
            f"[{'rete' if state.is_plugged_in else 'batteria'}"
            f"{f', {state.battery_percent:.0f}%' if state.battery_percent is not None else ''}]"
        )
        time.sleep(args.interval)

    orchestrator.flush()

    _title("Riepilogo")
    print(f"{INFO} tick eseguiti: {ticks} ({estimated_ticks} con potenza stimata)")
    for source, count in sorted(sources.items()):
        print(f"{INFO} attribuzione '{source}': {count} tick")

    totals = orchestrator.db.sum_energy_events_for_day(datetime.now().date())
    trace = orchestrator.db.iter_attribution_trace()
    aggregated_ticks = sum(event.n_samples or 1 for event in trace)
    print(
        f"{INFO} nel database: {totals['n_events']} righe aggregate "
        f"({aggregated_ticks} tick), {totals['energy_wh']:.4f} Wh, "
        f"{totals['operational_gco2']:.4f} gCO2"
    )
    print(f"{INFO} ricariche registrate: {orchestrator.db.count_charge_events()}")
    print(f"{INFO} campioni di rete salvati: {len(orchestrator.db.recent_grid_intensities())}")

    #il controllo e' sui tick, non sulle righe: gli eventi sono aggregati prima
    #di essere scritti, quindi pretendere una riga per tick fallirebbe sempre
    if aggregated_ticks >= ticks - 1:
        _line(
            OK, f"nessun tick perso nell'aggregazione ({totals['n_events']} righe per {ticks} tick)"
        )
    else:
        _line(FAIL, f"attesi ~{ticks} tick nelle righe salvate, contati {aggregated_ticks}")

    energy_from_trace = sum(event.wh for event in trace)
    if abs(energy_from_trace - totals["energy_wh"]) < 1e-9:
        _line(OK, "i Wh delle righe aggregate coincidono con il totale del giorno")
    else:
        _line(
            FAIL,
            f"somma delle righe {energy_from_trace:.6f} Wh != totale {totals['energy_wh']:.6f} Wh",
        )

    #riapertura: simula il riavvio dell'applicazione
    reopened = Orchestrator(db=LocalDatabase(db_path))
    live_pool = orchestrator.battery_pool
    restored_pool = reopened.battery_pool
    if (
        abs(restored_pool.stored_percent - live_pool.stored_percent) < 1e-6
        and abs(restored_pool.avg_intensity_g_per_kwh - live_pool.avg_intensity_g_per_kwh) < 1e-6
    ):
        _line(OK, "dopo la riapertura lo stato del pool e' identico (persistenza verificata)")
    else:
        _line(FAIL, "lo stato del pool cambia dopo la riapertura dell'applicazione")

    if not args.db:
        print(f"\n{INFO} database temporaneo lasciato in {db_path} per ispezione manuale.")


def cmd_all(args: argparse.Namespace) -> None:
    cmd_env(args)
    cmd_sensors(args)
    cmd_grid(args)
    cmd_pool(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ecomonitor.diagnostics",
        description="Verifiche dell'applicazione su hardware reale.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_env = sub.add_parser("env", help="che macchina e' questa e quale scenario copre")
    p_env.set_defaults(func=cmd_env)

    p_sensors = sub.add_parser("sensors", help="domini di potenza CPU/GPU, campionati piu' volte")
    p_sensors.add_argument("--samples", type=int, default=3)
    p_sensors.add_argument(
        "--interval", type=float, default=2.0, help="secondi fra i campioni (minimo 1)"
    )
    p_sensors.set_defaults(func=cmd_sensors)

    p_grid = sub.add_parser("grid", help="ElectricityMaps: valore attuale, previsione, storico")
    p_grid.set_defaults(func=cmd_grid)

    p_pool = sub.add_parser("pool", help="attribuzione e persistenza (nessun hardware richiesto)")
    p_pool.set_defaults(func=cmd_pool)

    p_battery = sub.add_parser("battery", help="osserva i Delta%% reali collegando/scollegando")
    p_battery.add_argument("--minutes", type=float, default=5.0)
    p_battery.set_defaults(func=cmd_battery)

    p_live = sub.add_parser("live", help="orchestratore reale su database temporaneo")
    p_live.add_argument("--minutes", type=float, default=2.0)
    p_live.add_argument("--interval", type=float, default=5.0)
    p_live.add_argument("--db", default=None, help="usa questo file invece di uno temporaneo")
    p_live.set_defaults(func=cmd_live)

    p_all = sub.add_parser("all", help="env + sensors + grid + pool (non interattivi)")
    p_all.add_argument("--samples", type=int, default=3)
    p_all.add_argument("--interval", type=float, default=2.0)
    p_all.set_defaults(func=cmd_all)

    global _failures, _warnings
    _failures = _warnings = 0

    args = parser.parse_args(argv)
    args.func(args)

    print()
    if _failures:
        print(f"ESITO: {_failures} controllo/i FALLITO/I, {_warnings} avviso/i")
        return 1
    print(f"ESITO: nessun fallimento, {_warnings} avviso/i")
    return 0


if __name__ == "__main__":
    sys.exit(main())
