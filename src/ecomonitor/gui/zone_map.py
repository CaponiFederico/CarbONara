import math

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ecomonitor.gui.theme import LIGHT, Theme
from ecomonitor.gui.zone_map_data import (
    ZONE_LABEL_POINTS,
    ZONE_NAMES,
    ZONE_ORDER,
    ZONE_POLYGONS,
    ZONE_REGIONS,
)

#Cartina cliccabile

_MARGIN = 6.0
#sotto questa larghezza no nomi
_MIN_WIDTH_FOR_LABELS = 190
_MIN_SIZE = QSize(200, 260)
_LABEL_BOX = QSize(120, 16)


def _longitude_scale() -> float:
    latitudes = [
        point[1] for polygons in ZONE_POLYGONS.values() for ring in polygons for point in ring
    ]
    middle = (min(latitudes) + max(latitudes)) / 2.0
    return math.cos(math.radians(middle))


_LONGITUDE_SCALE = _longitude_scale()


def _projected_bounds() -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for polygons in ZONE_POLYGONS.values():
        for ring in polygons:
            for longitude, latitude in ring:
                xs.append(longitude * _LONGITUDE_SCALE)
                ys.append(-latitude)
    return min(xs), min(ys), max(xs), max(ys)


_BOUNDS = _projected_bounds()


def _pen(color: QColor, width: float, dashed: bool = False) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


class ZoneMapWidget(QWidget):
    zone_changed = Signal(str)

    def __init__(
        self,
        selected_zone: str | None = None,
        parent: QWidget | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or LIGHT
        #una zona sconosciuta non seleziona niente
        self._selected = selected_zone if selected_zone in ZONE_POLYGONS else None
        self._hovered: str | None = None
        #ricostruiti al primo disegno e a ogni ridimensionamento
        self._paths: dict[str, QPainterPath] = {}
        self._label_points: dict[str, QPointF] = {}
        self._paths_for_size = QSize()

        self.setMinimumSize(_MIN_SIZE)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Cartina delle zone di mercato elettrico")
        self._refresh_accessible_text()


    @property
    def selected_zone(self) -> str | None:
        return self._selected

    def set_selected_zone(self, zone: str) -> None:
        if zone not in ZONE_POLYGONS or zone == self._selected:
            return
        self._selected = zone
        self._refresh_accessible_text()
        self.update()
        self.zone_changed.emit(zone)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def _refresh_accessible_text(self) -> None:
        #la cartina si legge anche senza vederla
        if self._selected is None:
            self.setAccessibleDescription(
                "Nessuna zona selezionata. Le zone sono "
                + ", ".join(ZONE_NAMES[zone] for zone in ZONE_ORDER)
                + ". Usa le frecce per sceglierne una."
            )
            return
        self.setAccessibleDescription(
            f"Zona selezionata: {ZONE_NAMES[self._selected]} ({self._selected}), "
            f"cioe' {ZONE_REGIONS[self._selected]}. Usa le frecce per cambiarla."
        )

    def _build_paths(self) -> None:
        left, top, right, bottom = _BOUNDS
        span_x = right - left
        span_y = bottom - top
        available_width = max(1.0, self.width() - 2 * _MARGIN)
        available_height = max(1.0, self.height() - 2 * _MARGIN)
        scale = min(available_width / span_x, available_height / span_y)
        #centrata nel widget, con margine minimo
        offset_x = _MARGIN + (available_width - span_x * scale) / 2.0
        offset_y = _MARGIN + (available_height - span_y * scale) / 2.0

        def to_screen(longitude: float, latitude: float) -> QPointF:
            return QPointF(
                offset_x + (longitude * _LONGITUDE_SCALE - left) * scale,
                offset_y + (-latitude - top) * scale,
            )

        self._paths = {}
        self._label_points = {}
        for zone, polygons in ZONE_POLYGONS.items():
            path = QPainterPath()
            for ring in polygons:
                path.moveTo(to_screen(*ring[0]))
                for point in ring[1:]:
                    path.lineTo(to_screen(*point))
                path.closeSubpath()
            self._paths[zone] = path
            self._label_points[zone] = to_screen(*ZONE_LABEL_POINTS[zone])
        self._paths_for_size = self.size()

    def _ensure_paths(self) -> None:
        if not self._paths or self._paths_for_size != self.size():
            self._build_paths()

    def zone_at(self, position: QPointF) -> str | None:
        self._ensure_paths()
        for zone in ZONE_ORDER:
            if self._paths[zone].contains(position):
                return zone
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hovered = self.zone_at(event.position())
        if hovered == self._hovered:
            return
        self._hovered = hovered
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if hovered else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        if self._hovered is not None:
            self._hovered = None
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        zone = self.zone_at(event.position())
        if zone is not None:
            self.set_selected_zone(zone)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        #le frecce scorrono le zone da nord a sud, come fossero una lista
        step = {
            int(Qt.Key.Key_Down): 1,
            int(Qt.Key.Key_Right): 1,
            int(Qt.Key.Key_Up): -1,
            int(Qt.Key.Key_Left): -1,
        }.get(event.key())
        if step is None:
            super().keyPressEvent(event)
            return

        if self._selected is None:
            self.set_selected_zone(ZONE_ORDER[0])
            return
        index = (ZONE_ORDER.index(self._selected) + step) % len(ZONE_ORDER)
        self.set_selected_zone(ZONE_ORDER[index])

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            zone = self.zone_at(QPointF(event.pos()))  # type: ignore[attr-defined]
            if zone is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(
                    event.globalPos(),  # type: ignore[attr-defined]
                    f"<qt><b>{ZONE_NAMES[zone]}</b> ({zone})<br>{ZONE_REGIONS[zone]}</qt>",
                    self,
                )
            return True
        return super().event(event)

    def _fill_color(self, zone: str) -> QColor:
        if zone == self._selected:
            return QColor(self._theme.accent)
        if zone == self._hovered:
            return QColor(self._theme.accent_soft)
        return QColor(self._theme.neutral_soft)

    def _text_color(self, zone: str) -> QColor:
        if zone == self._selected:
            return QColor(self._theme.card)
        if zone == self._hovered:
            return QColor(self._theme.accent)
        return QColor(self._theme.text_muted)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._ensure_paths()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        for zone in ZONE_ORDER:
            border = QColor(self._theme.text_muted)
            border.setAlpha(130)
            width = 1.0
            if zone == self._selected:
                border = QColor(self._theme.accent)
                width = 2.0
            elif zone == self._hovered:
                border = QColor(self._theme.accent)
            painter.setPen(_pen(border, width))
            painter.setBrush(self._fill_color(zone))
            painter.drawPath(self._paths[zone])

        if self.width() >= _MIN_WIDTH_FOR_LABELS:
            label_font = QFont(self.font())
            label_font.setPointSizeF(8.0)
            painter.setFont(label_font)
            for zone in ZONE_ORDER:
                painter.setPen(_pen(self._text_color(zone), 1.0))
                center = self._label_points[zone]
                box = QRectF(
                    center.x() - _LABEL_BOX.width() / 2.0,
                    center.y() - _LABEL_BOX.height() / 2.0,
                    float(_LABEL_BOX.width()),
                    float(_LABEL_BOX.height()),
                )
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter, ZONE_NAMES[zone])

        if self.hasFocus():
            painter.setPen(_pen(QColor(self._theme.accent), 1.0, dashed=True))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(self.rect().adjusted(0, 0, -1, -1)))

        painter.end()
