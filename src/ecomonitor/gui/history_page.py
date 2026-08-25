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
from ecomonitor.gui.labels import short_source_label
from ecomonitor.gui.theme import LIGHT, Theme
from ecomonitor.history import (
    MIN_COMPARABLE_SECONDS,
    Period,
    RangeSummary,
    build_range_summary,
)
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
    "quality": (
        "Quanta dell'energia del periodo viene da un contatore hardware e quanta "
        "dal modello di ripiego."
    ),
    "bias": (
        "Il confronto fra la potenza media stimata dal modello e quella misurata."
    ),
    "intensity": (
        "L'intensità media effettivamente pagata, pesata sull'energia e "
        "confrontata con la media della rete nello stesso periodo."
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

        cards = [
            ("energia", _HELP["energy"], "energy_label"),
            ("CO2eq", _HELP["gco2"], "gco2_label"),
            ("potenza media", _HELP["mean_power"], "mean_power_label"),
            ("copertura", _HELP["coverage"], "coverage_label"),
        ]
        for index, (caption, help_text, attribute) in enumerate(cards):
            card, value = self._metric_card(caption, help_text)
            setattr(self, attribute, value)
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
    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.profile_chart.set_theme(theme)
        self.quality_bar.set_theme(theme)
        self.provenance_bar.set_theme(theme)

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
        self.quality_bar.set_segments([])
        self.quality_legend.setText("")
        self.bias_label.setText("")
        self.intensity_label.setText("")
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
        self._render_quality(summary)
        self._render_provenance(summary)

    def _render_profile(self, summary: RangeSummary) -> None:
        self.profile_chart.set_bars(
            [
                Bar(
                    label=f"{slot.hour:02d}",
                    value=slot.mean_w,
                    caption=slot.caption,
                    tone="accent",
                )
                for slot in summary.hours
            ],
            label_every=3,
        )

        counted = sum(1 for slot in summary.hours if slot.has_value)
        skipped = sum(slot.days_partial for slot in summary.hours)
        parts = [f"{counted} fasce su 24 hanno almeno un giorno osservato per intero"]
        if skipped:
            parts.append(f"{skipped} osservazioni orarie escluse perché coperte solo in parte")
        equivalent = format_phone_charges(summary.energy_wh / WH_PER_PHONE_CHARGE)
        parts.append(f"nel periodo, {equivalent} ricariche di smartphone")
        self.profile_note.setText(" · ".join(parts) + ".")

    def _render_quality(self, summary: RangeSummary) -> None:
        self.quality_bar.set_segments(
            [
                Segment("misurato", summary.measured_wh, "success"),
                Segment("stimato", summary.estimated_wh, "warning"),
            ]
        )

        share = summary.measured_share
        if share is None:
            self.quality_legend.setText("")
        else:
            self.quality_legend.setText(
                f"{format_energy(summary.measured_wh)} da contatore hardware "
                f"({share * 100:.0f}%) · {format_energy(summary.estimated_wh)} da modello"
            )

        bias = summary.estimated_bias_ratio
        if bias is None and not summary.has_both_populations:
            self.bias_label.setText(
                "Confronto fra stima e misura non disponibile: nel periodo c'è un solo "
                "tipo di dato."
            )
        elif bias is None:
            #entrambe presenti ma troppo corte
            minutes = MIN_COMPARABLE_SECONDS / 60.0
            self.bias_label.setText(
                f"Tempo di misura insufficiente per confrontare stima e misura: servono almeno "
                f"{minutes:.0f} minuti per ciascun tipo. Finora: "
                f"{format_duration(summary.measured_seconds)} misurati e "
                f"{format_duration(summary.estimated_seconds)} stimati."
            )
        else:
            direction = "sopra" if bias > 0 else "sotto"
            self.bias_label.setText(
                f"Il modello di ripiego stima {abs(bias) * 100:.0f}% {direction} la misura "
                f"reale su questa macchina ({summary.mean_w_estimated:.1f} W contro "
                f"{summary.mean_w_measured:.1f} W medi)."
            )

        applied = summary.applied_intensity
        delta = summary.intensity_delta
        if applied is None:
            self.intensity_label.setText("")
        elif delta is None:
            self.intensity_label.setText(
                f"Intensità media pagata: {applied:.0f} gCO2/kWh. Nessun campione di rete "
                "nel periodo con cui confrontarla."
            )
        elif abs(delta) <= _INTENSITY_NOISE_RATIO * (summary.grid_mean_intensity or 0.1):
            #sotto questo scarto idue valori sono lo stesso numero con arrotondamenti diversi
            self.intensity_label.setText(
                f"Intensità media pagata: {applied:.0f} gCO2/kWh, in linea con la media "
                f"della rete nel periodo."
            )
        else:
            verdict = "più pulita" if delta < 0 else "più sporca"
            self.intensity_label.setText(
                f"Intensità media pagata: {applied:.0f} gCO2/kWh, contro "
                f"{summary.grid_mean_intensity:.0f} di media della rete: hai consumato "
                f"quando la rete era {verdict} del suo solito ({delta:+.0f})."
            )

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
