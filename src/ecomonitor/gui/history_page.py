import html
import logging
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ecomonitor.gui.charts import Bar, BarChart, CompositionBar, Segment
from ecomonitor.gui.theme import LIGHT, Theme
from ecomonitor.history import (
    MIN_AVOIDED_ROWS,
    MIN_COMPARABLE_SECONDS,
    AvoidedEmissions,
    Period,
    RangeSummary,
    build_range_summary,
)
from ecomonitor.labels import short_source_label
from ecomonitor.reporting import (
    WH_PER_PHONE_CHARGE,
    format_duration,
    format_energy,
    format_gco2,
    format_phone_charges,
)
from ecomonitor.storage.db import LocalDatabase, default_database_path

logger = logging.getLogger(__name__)

#Pagina di storico legge solo dal database
_HELP = {
    "period": (
        "Il periodo su cui sono calcolati tutti i numeri della pagina."
    ),
    "reload": (
        "Rilegge i dati dal database."
    ),
    "energy": (
        "Energia totale consumata dal computer nel periodo, misurata o stimata. "
    ),
    "gco2": "Grammi di CO2 attribuiti a quell'energia, con intensità del momento in cui è stata consumata.",
    "mean_power": (
        "Potenza media sulle sole ore osservate. Le ore non coperte "
        "dall'applicazione non abbassano la media."
    ),
    "coverage": (
        "Percentuale di tempo in cui l'applicazione ha potuto osservare il consumo del computer."
    ),
    "profile": (
        "Il consumo medio in ciascuna fascia oraria della giornata."
        " Le fasce senza valore non sono state osservate abbastanza a lungo."
    ),
    "grid_profile": (
        "L'intensità media della rete in ciascuna fascia oraria del periodo. "
        "Le fasce più basse sono quelle in cui conviene consumare."
    ),
    "quality": (
        "Quanta dell'energia del periodo viene da un contatore hardware e quanta "
        "dal modello di ripiego."
    ),
    "bias": (
        "Il confronto fra la potenza media stimata dal modello e quella misurata."
    ),
    "intensity": (
        "L'intensità media effettivamente pagata, pesata sull'energia."
    ),
    "avoided": (
        "Quanto è costata l'energia presa dalla batteria rispetto a quanto sarebbe "
        "costata tirandola dalla presa nello stesso istante."
    ),
    "provenance": (
        "Da dove viene l'attribuzione di CO2 dell'energia del periodo."
    ),
}

#Ogni quanto rileggere mentre la pagina si vede
#Coincide condb_flush_interval_seconds
DEFAULT_REFRESH_SECONDS = 60.0

AUTO_REFRESH_PERIODS = frozenset({Period.TODAY})

#Scarto relativo sotto cui il rumore del modello di ripiego non viene considerato significativo
_INTENSITY_NOISE_RATIO = 0.01

#quota di stime oltre la quale una fascia oraria e' considerata "per lo piu' stimata"
_MOSTLY_ESTIMATED = 0.50

_EMPTY_TEXT = (
    "Nessun dato salvato per questo periodo.\n"
    "I numeri compaiono dopo il primo salvataggio su database, "
    "che avviene circa un minuto dopo l'avvio."
)


class _LoaderSignals(QObject):
    #QRunnable non e' un QObject e non puo' avere segnali 
    ready = Signal(object)
    failed = Signal(str)


class _SummaryLoader(QRunnable):
    #La query sta fuori dal thread della GUI

    def __init__(self, path: Path, period: Period, signals: _LoaderSignals) -> None:
        super().__init__()
        self._path = path
        self._period = period
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            summary = build_range_summary(LocalDatabase(self._path), self._period)
        except Exception as exc:  # il thread di servizio non deve morire in silenzio
            logger.exception("Lettura dello storico non riuscita")
            self._deliver(self._signals.failed, str(exc))
            return
        self._deliver(self._signals.ready, summary)

    @staticmethod
    def _deliver(signal, payload) -> None:
        #Applicazione muore mentre una lettura e' in corso
        try:
            signal.emit(payload)
        except RuntimeError:
            logger.debug("Storico: risposta scartata, la pagina non esiste piu'")


class HistoryPage(QWidget):
    back_requested = Signal()

    def __init__(
        self,
        database_path: Path | None = None,
        theme: Theme | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or LIGHT
        self._database_path = database_path
        self._pool = QThreadPool.globalInstance()
        self._signals = _LoaderSignals()
        self._signals.ready.connect(self._on_ready)
        self._signals.failed.connect(self._on_failed)

        self._period = Period.LAST_7_DAYS
        self._pending = self._period
        self._loading = False

        #Rilettura periodica mentre la pagina si vede
        self._refresh_interval_ms = int(DEFAULT_REFRESH_SECONDS * 1000)
        self._refresh_timer = QTimer(self)  # con parent, altrimenti verrebbe raccolto
        self._refresh_timer.timeout.connect(self._auto_refresh)

        self._subtitle = QLabel()
        self._subtitle.setObjectName("pageSubtitle")
        self._subtitle.setWordWrap(True)

        root = QFrame(self)
        root.setObjectName("historyRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)

        layout.addLayout(self._build_header())
        layout.addWidget(self._subtitle)
        layout.addLayout(self._build_metrics())
        layout.addWidget(self._build_profile())
        layout.addWidget(self._build_grid_profile())
        layout.addWidget(self._build_quality())
        layout.addWidget(self._build_provenance())
        layout.addStretch(1)

        self._content = root
        scroll = QScrollArea(self)
        scroll.setWidget(root)
        scroll.setWidgetResizable(True)  # senza, il contenuto resta largo quanto il suo sizeHint
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._show_empty(_EMPTY_TEXT)

    def sizeHint(self) -> QSize:
        return self._content.sizeHint()

    # -- costruzione

    @staticmethod
    def _describe(widget: QWidget, help_text: str) -> None:
        widget.setToolTip(f"<qt>{help_text}</qt>")
        widget.setAccessibleDescription(help_text)

    def _build_header(self) -> QHBoxLayout:
        back = QPushButton("‹ Live")
        back.setObjectName("navButton")
        back.setAccessibleName("Torna alla schermata live")
        back.clicked.connect(self.back_requested.emit)

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._period_buttons: dict[Period, QPushButton] = {}

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(back)
        row.addSpacing(8)

        for period in Period:
            button = QPushButton(period.label)
            button.setObjectName("periodButton")
            button.setCheckable(True)
            button.setChecked(period is self._period)
            button.setAccessibleName(f"Periodo: {period.label}")
            self._describe(button, _HELP["period"])
            #chosen come default dell'argomento, cosi' il lambda 
            # lo cattura e non il valore finale di period
            button.clicked.connect(lambda _checked, chosen=period: self.select_period(chosen))
            self._buttons.addButton(button)
            self._period_buttons[period] = button
            row.addWidget(button)

        row.addStretch(1)

        self.reload_button = QPushButton("Aggiorna")
        self.reload_button.setObjectName("navButton")
        self.reload_button.setAccessibleName("Rileggi i dati dal database")
        self._describe(self.reload_button, _HELP["reload"])
        self.reload_button.clicked.connect(self.reload)
        row.addWidget(self.reload_button)
        return row

    def _metric_card(self, caption: str, help_text: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("metricCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(2)

        label = QLabel(caption)
        label.setObjectName("cardLabel")
        value = QLabel("—")
        value.setObjectName("cardValue")
        value.setAccessibleName(caption)

        card_layout.addWidget(label)
        card_layout.addWidget(value)
        for widget in (card, label, value):
            self._describe(widget, help_text)
        return card, value

    def _build_metrics(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        #Assegnati uno per uno
        cards = [
            self._metric_card("energia", _HELP["energy"]),
            self._metric_card("CO2eq", _HELP["gco2"]),
            self._metric_card("potenza media", _HELP["mean_power"]),
            self._metric_card("copertura", _HELP["coverage"]),
        ]
        self.energy_label = cards[0][1]
        self.gco2_label = cards[1][1]
        self.mean_power_label = cards[2][1]
        self.coverage_label = cards[3][1]

        for index, (card, _value) in enumerate(cards):
            grid.addWidget(card, index // 2, index % 2)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return grid

    def _section(self, title: str, help_text: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("metricCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        for widget in (frame, heading):
            self._describe(widget, help_text)
        return frame, layout

    def _build_profile(self) -> QFrame:
        frame, layout = self._section("Consumo medio per fascia oraria", _HELP["profile"])

        #La barra e' un grafico a barre, non una barra a segmenti: ogni fascia oraria e' un segmento verticale
        self.profile_chart = BarChart("W", theme=self._theme)
        layout.addWidget(self.profile_chart)

        self.profile_note = QLabel()
        self.profile_note.setObjectName("sectionNote")
        self.profile_note.setWordWrap(True)
        layout.addWidget(self.profile_note)
        return frame

    def _build_grid_profile(self) -> QFrame:
        frame, layout = self._section(
            "Intensità della rete per fascia oraria", _HELP["grid_profile"]
        )

        self.grid_profile_chart = BarChart("gCO2/kWh", theme=self._theme)
        layout.addWidget(self.grid_profile_chart)

        self.grid_profile_note = QLabel()
        self.grid_profile_note.setObjectName("sectionNote")
        self.grid_profile_note.setWordWrap(True)
        layout.addWidget(self.grid_profile_note)
        return frame

    def _build_quality(self) -> QFrame:
        frame, layout = self._section("Misura reale e stima", _HELP["quality"])

        self.quality_bar = CompositionBar(theme=self._theme)
        layout.addWidget(self.quality_bar)

        self.quality_legend = QLabel()
        self.quality_legend.setObjectName("legendText")
        self.quality_legend.setWordWrap(True)
        layout.addWidget(self.quality_legend)

        self.bias_label = QLabel()
        self.bias_label.setObjectName("sectionNote")
        self.bias_label.setWordWrap(True)
        self._describe(self.bias_label, _HELP["bias"])
        layout.addWidget(self.bias_label)

        self.intensity_label = QLabel()
        self.intensity_label.setObjectName("sectionNote")
        self.intensity_label.setWordWrap(True)
        self._describe(self.intensity_label, _HELP["intensity"])
        layout.addWidget(self.intensity_label)

        self.avoided_label = QLabel()
        self.avoided_label.setObjectName("sectionNote")
        self.avoided_label.setWordWrap(True)
        self._describe(self.avoided_label, _HELP["avoided"])
        layout.addWidget(self.avoided_label)
        return frame

    def _build_provenance(self) -> QFrame:
        frame, layout = self._section("Provenienza dell'attribuzione", _HELP["provenance"])

        self.provenance_bar = CompositionBar(theme=self._theme)
        layout.addWidget(self.provenance_bar)

        self.provenance_legend = QLabel()
        self.provenance_legend.setObjectName("legendText")
        self.provenance_legend.setWordWrap(True)
        layout.addWidget(self.provenance_legend)
        return frame

    # -- interazione
    def set_refresh_interval(self, seconds: float) -> None:
        #la passa la finestra, presa da db_flush_interval_seconds
        self._refresh_interval_ms = max(1000, int(seconds * 1000))
        if self._refresh_timer.isActive():
            self._refresh_timer.start(self._refresh_interval_ms)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reload()
        self._sync_timer()

    def hideEvent(self, event) -> None:
        #non legge in auto mentre la pagina e' nascosta
        super().hideEvent(event)
        self._refresh_timer.stop()

    def _sync_timer(self) -> None:
        #Il conto riparte da qui
        if self.isVisible() and self._period in AUTO_REFRESH_PERIODS:
            self._refresh_timer.start(self._refresh_interval_ms)
        else:
            self._refresh_timer.stop()

    def select_period(self, period: Period) -> None:
        self._period = period
        for candidate, button in self._period_buttons.items():
            button.setChecked(candidate is period)
        self.reload()
        self._sync_timer()

    def _auto_refresh(self) -> None:
        #una lettura ancora in corso salta il giro
        if self._loading:
            return
        self.reload(announce=False)

    def reload(self, announce: bool = True) -> None:
        self._pending = self._period
        self._loading = True
        if announce:
            self._subtitle.setText(f"{self._period.description} · lettura in corso…")
        path = self._database_path or default_database_path()
        self._pool.start(_SummaryLoader(path, self._period, self._signals))

    @Slot(object)
    def _on_ready(self, summary: RangeSummary) -> None:
        self._loading = False
        if summary.period is not self._pending:
            return  # risposta di una richiesta gia' superata da un altro clic
        self._render(summary)

    @Slot(str)
    def _on_failed(self, reason: str) -> None:
        self._loading = False
        self._subtitle.setText(f"{self._period.description} · lettura non riuscita")
        self._show_empty(f"Non è stato possibile leggere lo storico: {reason}")

    # -- disegno

    def _show_empty(self, text: str) -> None:
        for label in (
            self.energy_label,
            self.gco2_label,
            self.mean_power_label,
            self.coverage_label,
        ):
            label.setText("—")
        self.profile_chart.set_bars([], label_every=6)
        self.profile_note.setText(text)
        self.grid_profile_chart.set_bars([], label_every=6)
        self.grid_profile_note.setText("")
        self.quality_bar.set_segments([])
        self.quality_legend.setText("")
        self._set_note(self.bias_label, "", "bias")
        self._set_note(self.intensity_label, "", "intensity")
        self._set_note(self.avoided_label, "", "avoided")
        self.provenance_bar.set_segments([])
        self.provenance_legend.setText("")

    def _render(self, summary: RangeSummary) -> None:
        #l'ora della lettura fa parte del dato
        self._subtitle.setText(
            f"{summary.period.description} · dati dal database · "
            f"letti alle {summary.generated_at:%H:%M:%S}"
        )

        if not summary.has_data:
            self._show_empty(_EMPTY_TEXT)
            return

        self.energy_label.setText(format_energy(summary.energy_wh))
        self.gco2_label.setText(format_gco2(summary.operational_gco2))
        mean_w = summary.mean_w
        self.mean_power_label.setText("—" if mean_w is None else f"{mean_w:.1f} W")
        ratio = summary.coverage_ratio
        percent = "< 1%" if 0 < ratio < 0.005 else f"{ratio * 100:.0f}%"
        self.coverage_label.setText(f"{percent} · {format_duration(summary.observed_seconds)}")

        self._render_profile(summary)
        self._render_grid_profile(summary)
        self._render_quality(summary)
        self._render_avoided(summary)
        self._render_provenance(summary)

    def _render_profile(self, summary: RangeSummary) -> None:
        self.profile_chart.set_bars(
            [
                Bar(
                    label=f"{slot.hour:02d}",
                    value=slot.mean_w,
                    caption=slot.caption,
                    #la barra si divide in proporzione fra misurato e stimato
                    estimated_share=slot.estimated_share,
                )
                for slot in summary.hours
            ],
            label_every=3,
        )

        counted = sum(1 for slot in summary.hours if slot.has_value)
        skipped = sum(slot.days_partial for slot in summary.hours)
        with_estimates = [s for s in summary.hours if s.has_value and s.estimated_share > 0]
        mostly_estimated = sum(1 for s in with_estimates if s.estimated_share > _MOSTLY_ESTIMATED)
        parts = [f"{counted} fasce su 24 hanno almeno un giorno osservato per intero"]
        if with_estimates:
            note = f"{len(with_estimates)} contengono stime, in arancione"
            if mostly_estimated:
                note += f" ({mostly_estimated} per oltre metà)"
            parts.append(note)
        if skipped:
            parts.append(f"{skipped} osservazioni orarie escluse perché coperte solo in parte")
        equivalent = format_phone_charges(summary.energy_wh / WH_PER_PHONE_CHARGE)
        parts.append(f"nel periodo, {equivalent} ricariche di smartphone")
        self.profile_note.setText(" · ".join(parts) + ".")

    def _render_grid_profile(self, summary: RangeSummary) -> None:
        known = [slot for slot in summary.grid_hours if slot.has_value]
        self.grid_profile_chart.set_bars(
            [
                Bar(
                    label=f"{slot.hour:02d}",
                    value=slot.mean_g_per_kwh,
                    caption=slot.caption,
                    #piu' pulita del solito la fascia va in verde, piu' sporca in
                    #rosso
                    tone=self._grid_tone(slot, known),
                )
                for slot in summary.grid_hours
            ],
            label_every=3,
        )
        self.grid_profile_note.setText(self._grid_profile_note(known))

    @staticmethod
    def _grid_tone(slot, known: list) -> str:
        if not slot.has_value or len(known) < 2:
            return "muted"
        values = sorted(other.mean_g_per_kwh for other in known)
        #la fascia si giudica rispetto alle altre dello stesso periodo
        low = values[len(values) // 3]
        high = values[2 * len(values) // 3]
        if slot.mean_g_per_kwh <= low:
            return "success"
        if slot.mean_g_per_kwh >= high:
            return "danger"
        return "accent"

    @staticmethod
    def _grid_profile_note(known: list) -> str:
        if not known:
            return "Nessun campione di rete nel periodo."
        if len(known) < 2:
            return "Una sola fascia oraria con campioni: troppo poco per confrontarle."

        best = min(known, key=lambda slot: slot.mean_g_per_kwh)
        worst = max(known, key=lambda slot: slot.mean_g_per_kwh)
        ratio = worst.mean_g_per_kwh / best.mean_g_per_kwh if best.mean_g_per_kwh else 0.0
        return (
            f"La fascia più pulita è le {best.hour:02d}:00 con "
            f"{best.mean_g_per_kwh:.0f} gCO2/kWh, la più sporca le {worst.hour:02d}:00 con "
            f"{worst.mean_g_per_kwh:.0f}: un fattore {ratio:.1f} dentro la stessa giornata."
        )

    def _render_quality(self, summary: RangeSummary) -> None:
        self.quality_bar.set_segments(
            [
                Segment("misurato", summary.measured_wh, "success"),
                Segment("stimato", summary.estimated_wh, "warning"),
            ]
        )

        parts = []
        if summary.measured_wh > 0:
            parts.append(f"{format_energy(summary.measured_wh)} misurati")
        if summary.estimated_wh > 0:
            parts.append(f"{format_energy(summary.estimated_wh)} stimati")
        self.quality_legend.setText(" · ".join(parts))

        bias = summary.estimated_bias_ratio
        if bias is None and not summary.has_both_populations:
            #un solo tipo di dato: non c'e' niente da confrontare
            self._set_note(self.bias_label, "", "bias")
        elif bias is None:
            #entrambe presenti ma troppo corte
            minutes = MIN_COMPARABLE_SECONDS / 60.0
            self._set_note(
                self.bias_label,
                f"Stima e misura confrontabili dopo {minutes:.0f} minuti di ciascuna.",
                "bias",
                f"Finora: {format_duration(summary.measured_seconds)} misurati e "
                f"{format_duration(summary.estimated_seconds)} stimati.",
            )
        else:
            direction = "sopra" if bias > 0 else "sotto"
            self._set_note(
                self.bias_label,
                f"Stima {abs(bias) * 100:.0f}% {direction} la misura "
                f"({summary.mean_w_estimated:.1f} contro {summary.mean_w_measured:.1f} W).",
                "bias",
            )

        applied = summary.applied_intensity
        if applied is None:
            self._set_note(self.intensity_label, "", "intensity")
            return
        self._set_note(
            self.intensity_label,
            f"Intensità pagata: {applied:.0f} gCO2/kWh" + self._intensity_verdict(summary) + ".",
            "intensity",
            (self._intensity_comparison(summary) + self._battery_note(summary)).strip(),
        )

    def _set_note(self, label: QLabel, text: str, help_key: str, detail: str = "") -> None:
        #a vista la frase corta, la spiegazione intera nel suggerimento
        label.setText(text)
        label.setVisible(bool(text))
        full = f"{_HELP[help_key]}\n\n{detail}" if detail else _HELP[help_key]
        label.setToolTip("<qt>" + html.escape(full, quote=False).replace("\n", "<br>") + "</qt>")
        label.setAccessibleDescription(full)

    def _intensity_verdict(self, summary: RangeSummary) -> str:
        delta = summary.intensity_delta
        if delta is None:
            if summary.grid_wh > 0 and summary.grid_mean_intensity is not None:
                return " · rete osservata troppo poco per un confronto"
            return ""

        grid_applied = summary.applied_grid_intensity or 0.0
        mean = summary.grid_mean_intensity or 0.0
        if abs(delta) <= _INTENSITY_NOISE_RATIO * (mean or 0.1):
            verdict = f" · dalla rete {grid_applied:.0f}, in linea con la media"
        else:
            cleaner = "più pulita" if delta < 0 else "più sporca"
            verdict = f" · dalla rete {grid_applied:.0f} contro {mean:.0f} di media: {cleaner}"

        grams = summary.load_shift_gco2
        if grams is None:
            return verdict
        if grams >= 0:
            return f"{verdict}, {format_gco2(grams)} evitati"
        return f"{verdict}, {format_gco2(-grams)} in più"

    def _intensity_comparison(self, summary: RangeSummary) -> str:
        #Il confronto con la rete vale solo per l'energia presa dalla rete
        delta = summary.intensity_delta
        if delta is None:
            if summary.grid_wh <= 0:
                return (
                    "Nel periodo non c'è energia presa dalla rete: quella dalla batteria "
                    "porta l'intensità del momento in cui è stata caricata, e non si "
                    "confronta con la rete di adesso."
                )
            if summary.grid_mean_intensity is None:
                return "Nessun campione di rete nel periodo con cui confrontarla."
            return (
                f"La rete è nota per {self._grid_coverage_words(summary)} "
                f"su {format_duration(summary.elapsed_seconds)} del periodo "
                f"({summary.grid_coverage_ratio * 100:.0f}%): troppo poco per dire se hai "
                f"consumato meglio o peggio della sua media."
            )

        grid_applied = summary.applied_grid_intensity or 0.0
        mean = summary.grid_mean_intensity or 0.0
        observed = self._grid_coverage_words(summary)
        if abs(delta) <= _INTENSITY_NOISE_RATIO * (mean or 0.1):
            #sotto questo scarto i due valori sono lo stesso numero con arrotondamenti 
            return (
                f"Sull'energia presa dalla rete hai pagato {grid_applied:.0f}, in linea con "
                f"i {mean:.0f} medi della rete su {observed}."
            ) + self._load_shift_note(summary)

        verdict = "più pulita" if delta < 0 else "più sporca"
        return (
            f"Sull'energia presa dalla rete hai pagato {grid_applied:.0f} contro {mean:.0f} "
            f"medi della rete su {observed}: hai consumato quando era {verdict} "
            f"({delta:+.0f})."
        ) + self._load_shift_note(summary)

    def _grid_coverage_words(self, summary: RangeSummary) -> str:
        observed = summary.grid_observed_seconds
        reconstructed = summary.grid_reconstructed_seconds
        if reconstructed <= 0:
            return f"{format_duration(observed)} osservate"
        if observed <= 0:
            return f"{format_duration(reconstructed)} ricostruite"
        return (
            f"{format_duration(observed)} osservate e "
            f"{format_duration(reconstructed)} ricostruite"
        )

    def _load_shift_note(self, summary: RangeSummary) -> str:
        #Lo stesso scarto in grammi
        grams = summary.load_shift_gco2
        if grams is None:
            return ""

        energy = format_energy(summary.grid_wh)
        if grams >= 0:
            return (
                f" Su {energy} sono {format_gco2(grams)} evitati rispetto a consumare la "
                f"stessa energia a intensità media del periodo."
            )
        return (
            f" Su {energy} sono {format_gco2(-grams)} in più rispetto a consumare la "
            f"stessa energia a intensità media del periodo."
        )

    def _battery_note(self, summary: RangeSummary) -> str:
        #Le due popolazioni che il confronto lascia fuori, dichiarate solo se ci sono
        excluded = []
        if summary.pool_wh > 0:
            excluded.append(f"{format_energy(summary.pool_wh)} dalla batteria")
        if summary.fallback_wh > 0:
            excluded.append(f"{format_energy(summary.fallback_wh)} senza storico di ricarica")
        if not excluded:
            return ""
        return f" Fuori dal confronto: {' e '.join(excluded)}."

    def _render_avoided(self, summary: RangeSummary) -> None:
        #Il saldo fra quello che l'energia da batteria e' costata e quello che
        #sarebbe costata dalla presa nello stesso istante
        avoided = summary.avoided
        if not avoided.has_battery:
            #su una macchina sempre collegata il numero non esiste
            self._set_note(self.avoided_label, "", "avoided")
            return

        self._set_note(
            self.avoided_label,
            self._avoided_short(avoided),
            "avoided",
            self._avoided_verdict(avoided) + self._avoided_excluded(avoided),
        )

    @staticmethod
    def _avoided_short(avoided: AvoidedEmissions) -> str:
        if avoided.rows < MIN_AVOIDED_ROWS or not avoided.is_publishable:
            return "Saldo della batteria: dati insufficienti."
        balance = avoided.balance_gco2
        if abs(balance) < 1.0:
            return "Batteria: emissioni pari a quelle dalla presa."
        if balance > 0:
            return f"Batteria: {format_gco2(balance)} evitati rispetto alla presa."
        return f"Batteria: {format_gco2(-balance)} in più rispetto alla presa."

    def _avoided_verdict(self, avoided: AvoidedEmissions) -> str:
        if avoided.rows == 0:
            return (
                f"Nel periodo ci sono {format_energy(avoided.battery_wh)} presi dalla "
                f"batteria, ma nessuna riga ha accanto una lettura della rete con cui "
                f"confrontarli."
            )
        if avoided.rows < MIN_AVOIDED_ROWS:
            return (
                f"Solo {avoided.rows} righe a batteria hanno accanto una lettura della "
                f"rete, ne servono {MIN_AVOIDED_ROWS}: saldo non calcolato."
            )
        if not avoided.is_publishable:
            return (
                f"Solo il {avoided.matched_share * 100:.0f}% dell'energia presa dalla "
                f"batteria ha accanto una lettura della rete: il saldo descriverebbe un "
                f"ritaglio del consumo a batteria."
            )

        balance = avoided.balance_gco2
        energy = format_energy(avoided.wh)
        if abs(balance) < 1.0:
            return (
                f"I {energy} presi dalla batteria sono costati quanto sarebbero costati "
                f"dalla presa negli stessi istanti."
            )

        if balance > 0:
            verdict = (
                f"Usando i {energy} presi dalla batteria hai evitato "
                f"{format_gco2(balance)}: erano stati caricati quando la rete era più "
                f"pulita di quando li hai consumati."
            )
        else:
            verdict = (
                f"Usando i {energy} presi dalla batteria hai emesso "
                f"{format_gco2(-balance)} in più: erano stati caricati quando la rete era "
                f"più sporca di quando li hai consumati."
            )

        if avoided.saved_gco2 > 0 and avoided.lost_gco2 > 0:
            verdict += (
                f" Il saldo mette insieme {format_gco2(avoided.saved_gco2)} evitati e "
                f"{format_gco2(avoided.lost_gco2)} in più."
            )
        return verdict + " Al lordo delle perdite di carica e scarica."

    def _avoided_excluded(self, avoided: AvoidedEmissions) -> str:
        #Le due quote che il saldo non copre
        excluded = []
        if avoided.unmatched_wh > 0 and avoided.rows > 0:
            excluded.append(f"{format_energy(avoided.unmatched_wh)} senza lettura vicina")
        if avoided.partial_wh > 0:
            excluded.append(f"{format_energy(avoided.partial_wh)} senza storico di ricarica")
        if not excluded:
            return ""
        return f" Fuori dal saldo: {' e '.join(excluded)}."

    def _render_provenance(self, summary: RangeSummary) -> None:
        tones = {
            "grid_live": "accent",
            "battery_pool": "success",
            "battery_pool_partial": "warning",
            "battery_pool_fallback": "muted",
        }
        ordered = sorted(summary.source_wh.items(), key=lambda item: -item[1])
        self.provenance_bar.set_segments(
            [
                Segment(short_source_label(source), wh, tones.get(source, "muted"))
                for source, wh in ordered
            ]
        )

        total = sum(summary.source_wh.values())
        if total <= 0:
            self.provenance_legend.setText("")
            return
        self.provenance_legend.setText(
            " · ".join(
                f"{short_source_label(source)} {wh / total * 100:.0f}%"
                for source, wh in ordered
            )
        )
