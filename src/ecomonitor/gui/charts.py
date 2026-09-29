from dataclasses import dataclass

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ecomonitor.gui.theme import LIGHT, Theme

#Grafici 
_BAR_GAP = 2
_MIN_BAR_WIDTH = 3
_AXIS_TEXT_HEIGHT = 14
_VALUE_TEXT_HEIGHT = 13
_MIN_CHART_HEIGHT = 96


@dataclass(frozen=True)
class Bar:
    label: str
    value: float | None
    caption: str
    tone: str = "accent"

    #Quota della barra che viene da una stima invece che da un sensore
    estimated_share: float = 0.0


def _tone_color(theme: Theme, tone: str) -> QColor:
    return QColor(
        {
            "accent": theme.accent,
            "success": theme.success,
            "warning": theme.warning,
            "danger": theme.danger,
            "muted": theme.text_muted,
        }.get(tone, theme.accent)
    )


class BarChart(QWidget):
    #Barre verticali
    def __init__(
        self,
        unit: str = "W",
        parent: QWidget | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or LIGHT
        self._unit = unit
        self._bars: list[Bar] = []
        self._label_every = 1

        self.setMinimumHeight(_MIN_CHART_HEIGHT + _AXIS_TEXT_HEIGHT + _VALUE_TEXT_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        #serve al passaggio del mouse
        self.setMouseTracking(True)

    def set_bars(self, bars: list[Bar], label_every: int = 1) -> None:
        self._bars = bars
        self._label_every = max(1, label_every)
        self._refresh_accessible_text()
        self.update()

    #lettura senza mouse
    def _refresh_accessible_text(self) -> None:
        described = [bar for bar in self._bars if bar.value is not None]
        if not described:
            self.setAccessibleDescription("Nessun dato da mostrare in questo grafico.")
            return

        highest = max(described, key=lambda bar: bar.value or 0.0)
        lowest = min(described, key=lambda bar: bar.value or 0.0)
        self.setAccessibleDescription(
            f"{len(described)} barre su {len(self._bars)} hanno un valore. "
            f"Massimo {highest.label}: {highest.value:.1f} {self._unit}. "
            f"Minimo {lowest.label}: {lowest.value:.1f} {self._unit}. "
            f"Le fasce senza valore non sono state osservate abbastanza a lungo."
        )
        #la divisione della barra
        with_estimates = [bar for bar in described if bar.estimated_share > 0]
        if with_estimates:
            worst = max(with_estimates, key=lambda bar: bar.estimated_share)
            self.setAccessibleDescription(
                self.accessibleDescription()
                + f" {len(with_estimates)} contengono stime, al massimo il "
                f"{worst.estimated_share * 100:.0f}% in {worst.label}."
            )

    #geometria
    def _plot_rect(self) -> QRectF:
        return QRectF(
            0.0,
            float(_VALUE_TEXT_HEIGHT),
            float(self.width()),
            float(max(1, self.height() - _VALUE_TEXT_HEIGHT - _AXIS_TEXT_HEIGHT)),
        )

    def _bar_rect(self, index: int, maximum: float) -> QRectF | None:
        bar = self._bars[index]
        if bar.value is None:
            return None

        plot = self._plot_rect()
        slot = plot.width() / len(self._bars)
        width = max(_MIN_BAR_WIDTH, slot - _BAR_GAP)
        left = plot.left() + index * slot + (slot - width) / 2.0

        ratio = 0.0 if maximum <= 0 else max(0.0, bar.value) / maximum
        height = max(1.0, ratio * plot.height()) if bar.value > 0 else 1.0
        return QRectF(left, plot.bottom() - height, width, height)

    def _bar_at(self, x: float) -> Bar | None:
        if not self._bars:
            return None
        slot = self._plot_rect().width() / len(self._bars)
        index = int(x // slot) if slot > 0 else 0
        if 0 <= index < len(self._bars):
            return self._bars[index]
        return None

    #eventi
    def event(self, event) -> bool:
        #Il suggerimento delle singole barre
        if event.type() == QEvent.Type.ToolTip:
            return True
        return super().event(event)

    def mouseMoveEvent(self, event) -> None:
        bar = self._bar_at(event.position().x())
        if bar is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), bar.caption, self)
        super().mouseMoveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        plot = self._plot_rect()
        values = [bar.value for bar in self._bars if bar.value is not None]
        maximum = max(values) if values else 0.0

        #linea di base
        painter.setPen(QColor(self._theme.border))
        painter.drawLine(
            int(plot.left()), int(plot.bottom()), int(plot.right()), int(plot.bottom())
        )

        small = QFont(self.font())
        small.setPointSizeF(max(6.0, self.font().pointSizeF() - 2.0))
        painter.setFont(small)

        if maximum > 0:
            painter.setPen(QColor(self._theme.text_muted))
            painter.drawText(
                QRectF(0.0, 0.0, plot.width(), float(_VALUE_TEXT_HEIGHT)),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                f"max {maximum:.1f} {self._unit}",
            )

        missing_color = QColor(self._theme.border)
        for index, bar in enumerate(self._bars):
            rect = self._bar_rect(index, maximum)
            if rect is None:
                slot = plot.width() / len(self._bars)
                left = plot.left() + index * slot + slot / 2.0
                painter.setPen(missing_color)
                painter.drawLine(
                    int(left), int(plot.bottom()), int(left), int(plot.bottom() - 3)
                )
                continue

            painter.setPen(Qt.PenStyle.NoPen)
            base = _tone_color(self._theme, bar.tone)

            if bar.estimated_share <= 0.0:
                painter.setBrush(base)
                painter.drawRoundedRect(rect, 2.0, 2.0)
                continue

            #Barra divisa in proporzione
            shape = QPainterPath()
            shape.addRoundedRect(rect, 2.0, 2.0)
            painter.save()
            painter.setClipPath(shape)

            #almeno un pixel
            estimated_height = max(1.0, rect.height() * min(1.0, bar.estimated_share))
            measured_height = max(0.0, rect.height() - estimated_height)

            painter.fillRect(
                QRectF(rect.left(), rect.top(), rect.width(), measured_height), base
            )
            painter.fillRect(
                QRectF(
                    rect.left(),
                    rect.bottom() - estimated_height,
                    rect.width(),
                    estimated_height,
                ),
                _tone_color(self._theme, "warning"),
            )
            painter.restore()

        painter.setPen(QColor(self._theme.text_muted))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        slot = plot.width() / len(self._bars) if self._bars else 0.0
        for index, bar in enumerate(self._bars):
            if index % self._label_every:
                continue
            painter.drawText(
                QRectF(
                    plot.left() + index * slot - slot,
                    plot.bottom() + 1.0,
                    slot * 3.0,
                    float(_AXIS_TEXT_HEIGHT),
                ),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                bar.label,
            )
        painter.end()


@dataclass(frozen=True)
class Segment:
    label: str
    value: float
    tone: str


class CompositionBar(QWidget):
    #Barra orizzontale a segmenti

    _HEIGHT = 22
    _MIN_LABEL_RATIO = 0.04

    def __init__(self, parent: QWidget | None = None, theme: Theme | None = None) -> None:
        super().__init__(parent)
        self._theme = theme or LIGHT
        self._segments: list[Segment] = []
        self.setFixedHeight(self._HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_segments(self, segments: list[Segment]) -> None:
        self._segments = [segment for segment in segments if segment.value > 0]
        total = sum(segment.value for segment in self._segments)
        if total > 0:
            parts = [
                f"{segment.label} {segment.value / total * 100:.0f}%"
                for segment in self._segments
            ]
            self.setToolTip(" · ".join(parts))
            self.setAccessibleDescription("Composizione: " + ", ".join(parts))
        else:
            self.setToolTip("")
            self.setAccessibleDescription("Composizione non disponibile.")
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        total = sum(segment.value for segment in self._segments)
        if total <= 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self._theme.neutral_soft))
            painter.drawRoundedRect(QRectF(self.rect()), 4.0, 4.0)
            painter.end()
            return

        small = QFont(self.font())
        small.setPointSizeF(max(6.0, self.font().pointSizeF() - 2.0))

        left = 0.0
        for segment in self._segments:
            width = self.width() * segment.value / total
            rect = QRectF(left, 0.0, width, float(self.height()))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_tone_color(self._theme, segment.tone))
            painter.drawRect(rect)

            if segment.value / total >= self._MIN_LABEL_RATIO:
                painter.setFont(small)
                painter.setPen(QColor(self._theme.card))
                painter.drawText(
                    rect,
                    int(Qt.AlignmentFlag.AlignCenter),
                    f"{segment.value / total * 100:.0f}%",
                )
            left += width
        painter.end()
