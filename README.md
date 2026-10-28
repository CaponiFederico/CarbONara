# CarbO'Nara

**Quanta CO2 costa davvero il tuo computer, e quando conviene usarlo.**

Ogni ora della giornata ha diversi fattori che influiscono sul consumo di CO2:
dipende dal mix energetico che viene utilizzato in quel momento e cioé da quanto carbone,
gas, sole e vento ci sono nella rete in quel momento. CarbO'Nara misura i watt del
computer e assegna a ciascuno l'intensità di carbonio della rete nel momento in cui
è stato consumato. Su un portatile a batteria conta il momento della ricarica in quanto
viene riempito il serbatoio della batteria al momento della ricarica, per questo si
osserva il momento della ricarica e non quello dell'uso.

- **Misura**: Legge processore e scheda grafica dai sensori hardware.
  Se non può, stima e lo dichiara al posto di smettere di funzionare.
- **Quando**: Un podio delle tre ore migliori fra le prossime 8, con un
  consiglio che spiega se è meglio ricaricare o eseguire task pesanti in
  quel momento piuttosto che in altri.
- **Non Intrusivo**: Può lavorare nell'area di notifica e avvisare solo quando conviene.
- **Storico**: Energia, emissioni e profilo orario, tutto in locale.

---

## Installazione

| sistema | pacchetto | note |
| --- | --- | --- |
| **Windows 10/11** (x64) | `CarbONara-<versione>-windows-x64-setup.exe` | doppio clic |
| **macOS** (Apple Silicon) | `carbonara-<versione>-macos-arm64.pkg` | doppio clic |
| **Debian / Ubuntu** | `ecomonitor_<versione>_amd64.deb` | `sudo apt install ./ecomonitor_<versione>_amd64.deb` |
| **Fedora** | `ecomonitor-<versione>-1.<dist>.x86_64.rpm` | `sudo dnf install ./ecomonitor-<versione>-*.rpm` |
| **Altre distribuzioni Linux** | `carbonara-<versione>-linux-x86_64.tar.gz` | senza root, in `~/.local` |

**Windows.** L'installer non è firmato: SmartScreen può bloccarlo finché non si sceglie
*Ulteriori informazioni › Esegui comunque*. Per misurare la CPU serve il driver
PawnIO, che l'installer propone di scaricare e installare.

**macOS.** Al primo avvio può essere necessario aprirlo da *Impostazioni › Privacy e
sicurezza*. Il pacchetto autorizza da solo la lettura dei sensori (*powermetrics*).
Per disinstallare c'è *CarbONaraUninstall.command* in Applicazioni.

**Linux, `.tar.gz`:**

```bash
mkdir -p /tmp/carbonara && tar -xzf carbonara-<versione>-linux-x86_64.tar.gz -C /tmp/carbonara
bash /tmp/carbonara/install.sh
sudo bash /tmp/carbonara/scripts/install_rapl_permissions.sh   # per misurare la CPU invece di stimarla
```

`.deb` e `.rpm` installano da soli la regola che rende leggibili i contatori RAPL.

I dati restano sul computer, nella cartella *carbonara* dei dati utente, anche dopo
una disinstallazione.

---

## Dall'origine

Serve Python 3.11 o più recente.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[gui]"            # Windows: .venv\Scripts\pip install -e ".[gui,windows]"
.venv/bin/python -m ecomonitor.app
```

---

## Ricreare i pacchetti

Tutte le build vogliono l'indirizzo `https://` del server che fornisce i dati di rete,
in un file `.study_endpoint` nella radice del progetto (escluso da git) oppure nella
variabile `ECOMONITOR_STUDY_ENDPOINT`. Senza, si rifiutano di partire.

Il pacchetto si costruisce sul sistema a cui è destinato, con un venv che ha l'extra
`packaging`. Se l'ultima build in `packaging/dist/` era di un altro sistema, svuotala
prima.

**Windows**, con [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install --id JRSoftware.InnoSetup -e`):

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[gui,windows,packaging]"
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

**Linux**: prima l'archivio, poi, se servono, `.deb` e `.rpm` a partire da quello.
Sulla macchina di build serve `libxcb-cursor0`, altrimenti il pacchetto non parte
sotto X11.

```bash
.venv/bin/pip install -e ".[gui,packaging]"
bash packaging/build_linux.sh
bash packaging/build_deb.sh      # Debian/Ubuntu
bash packaging/build_rpm.sh      # Fedora, serve rpm-build
```

**macOS**, solo con un Python arm64:

```bash
.venv/bin/pip install -e ".[gui,macos,packaging]"
bash packaging/build_macos.sh
bash packaging/smoke_macos.sh    # controllo del pacchetto appena costruito
```

I pacchetti finiscono in `packaging/out/`.

---

## Appendice: licenza

Il codice di CarbO'Nara è distribuito con licenza **MIT**: vedi [LICENSE](LICENSE).

I pacchetti includono componenti di terzi, ciascuno con la propria licenza:

| componente | licenza |
|---|---|
| Qt for Python (PySide6, shiboken6) | LGPL-3.0, collegata dinamicamente |
| LibreHardwareMonitor, tramite HardwareMonitor (solo Windows) | MPL-2.0 / BSD-3-Clause |
| pythonnet, platformdirs, PyWinRT, PyObjC | MIT |
| psutil, httpx | BSD-3-Clause |
| PyInstaller (solo per costruire; il bootloader incluso ha un'eccezione) | GPL-2.0 con eccezione |

Il driver **PawnIO** non è incluso: l'installer Windows lo scarica dalla release
ufficiale, se lo scegli. I confini della cartina vengono da **Natural Earth**, di
pubblico dominio. I dati di intensità della rete sono di **Electricity Maps**, letti
dal server del progetto.
