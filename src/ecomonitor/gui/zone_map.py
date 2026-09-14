import math
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
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
    COUNTRY_INSETS,
    COUNTRY_NAMES,
    COUNTRY_ZONES,
    MARKER_ZONES,
    ZONE_LABEL_POINTS,
    ZONE_NAMES,
    ZONE_POLYGONS,
    ZONE_REGIONS,
)

#Cartina cliccabile, un paese europeo alla volta

_MARGIN = 6.0
#sotto questa larghezza no nomi
_MIN_WIDTH_FOR_LABELS = 190
_MIN_SIZE = QSize(200, 260)
_LABEL_HEIGHT = 16.0
_LABEL_MIN_WIDTH_RATIO = 0.6


def normalize_country(code: str | None) -> str:
    return (code or "").strip().upper()


def country_of_zone(zone: str | None) -> str | None:
    #il paese a cui Electricity Maps assegna la zona
    for country, zones in COUNTRY_ZONES.items():
        if zone in zones:
            return country
    return None


@dataclass(frozen=True)
class _Frame:
    longitude_scale: float
    left: float
    top: float
    right: float
    bottom: float


_FRAMES: dict[str, _Frame] = {}


def _frame_for(country: str) -> _Frame:
    frame = _FRAMES.get(country)
    if frame is not None:
        return frame
    points = [
        point
        for zone in COUNTRY_ZONES[country]
        for ring in ZONE_POLYGONS[zone]
        for point in ring
    ]
    for west, south, east, north in COUNTRY_INSETS.get(country, ()):
        points += [(west, south), (east, north)]
    latitudes = [latitude for _, latitude in points]
    scale = math.cos(math.radians((min(latitudes) + max(latitudes)) / 2.0))
    xs = [longitude * scale for longitude, _ in points]
    ys = [-latitude for _, latitude in points]
    frame = _Frame(scale, min(xs), min(ys), max(xs), max(ys))
    _FRAMES[country] = frame
    return frame


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
        country: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or LIGHT
        #senza un paese dichiarato vale quello della zona
        self._requested_country = (
            normalize_country(country)
            if country is not None
            else country_of_zone(selected_zone) or ""
        )
        self._country = (
            self._requested_country if self._requested_country in COUNTRY_ZONES else None
        )
        #una zona sconosciuta, o di un altro paese, non seleziona niente
        self._selected = selected_zone if selected_zone in self.zones else None
        self._hovered: str | None = None
        #ricostruiti al primo disegno, a ogni ridimensionamento e a ogni paese
        self._paths: dict[str, QPainterPath] = {}
        self._label_points: dict[str, QPointF] = {}
        self._inset_rects: list[QRectF] = []
        self._paths_for: tuple[QSize, str | None] = (QSize(), None)

        self.setMinimumSize(_MIN_SIZE)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Cartina delle zone di mercato elettrico")
        self._refresh_accessible_text()

    @property
    def selected_zone(self) -> str | None:
        return self._selected

    @property
    def country(self) -> str | None:
        #None quando per il codice scritto non c'e' una cartina
        return self._country

    @property
    def zones(self) -> tuple[str, ...]:
        return COUNTRY_ZONES.get(self._country or "", ())

    def set_country(self, code: str | None) -> None:
        requested = normalize_country(code)
        if requested == self._requested_country:
            return
        self._requested_country = requested
        country = requested if requested in COUNTRY_ZONES else None
        picked: str | None = None
        if country != self._country:
            self._country = country
            self._hovered = None
            if self._selected not in self.zones:
                self._selected = None
            #un paese con una zona sola non ha niente da scegliere
            if self._selected is None and len(self.zones) == 1:
                self._selected = picked = self.zones[0]
        self._refresh_accessible_text()
        self.update()
        if picked is not None:
            self.zone_changed.emit(picked)

    def set_selected_zone(self, zone: str) -> None:
        if zone not in self.zones or zone == self._selected:
            return
        self._selected = zone
        self._refresh_accessible_text()
        self.update()
        self.zone_changed.emit(zone)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def empty_message(self) -> str:
        if not self._requested_country:
            return "Scrivi il codice di un paese europeo per vedere la sua cartina."
        return (
            f"Nessuna cartina per «{self._requested_country}». "
            "Sono disponibili i paesi europei, per codice ISO a due lettere."
        )

    def _refresh_accessible_text(self) -> None:
        #la cartina si legge anche senza vederla
        if self._country is None:
            self.setAccessibleDescription(self.empty_message())
            return
        if self._selected is None:
            self.setAccessibleDescription(
                f"{COUNTRY_NAMES[self._country]}, nessuna zona selezionata. Le zone sono "
                + ", ".join(ZONE_NAMES[zone] for zone in self.zones)
                + ". Usa le frecce per sceglierne una."
            )
            return
        regions = ZONE_REGIONS.get(self._selected)
        detail = f", cioe' {regions}" if regions else ""
        self.setAccessibleDescription(
            f"Zona selezionata: {ZONE_NAMES[self._selected]} ({self._selected}){detail}. "
            "Usa le frecce per cambiarla."
        )

    def _draw_order(self) -> tuple[str, ...]:
        return tuple(zone for zone in self.zones if zone not in MARKER_ZONES) + tuple(
            zone for zone in self.zones if zone in MARKER_ZONES
        )

    def _build_paths(self) -> None:
        self._paths = {}
        self._label_points = {}
        self._inset_rects = []
        self._paths_for = (self.size(), self._country)
        if self._country is None:
            return

        frame = _frame_for(self._country)
        span_x = max(frame.right - frame.left, 1e-9)
        span_y = max(frame.bottom - frame.top, 1e-9)
        available_width = max(1.0, self.width() - 2 * _MARGIN)
        available_height = max(1.0, self.height() - 2 * _MARGIN)
        scale = min(available_width / span_x, available_height / span_y)
        #centrata nel widget, con margine minimo
        offset_x = _MARGIN + (available_width - span_x * scale) / 2.0
        offset_y = _MARGIN + (available_height - span_y * scale) / 2.0

        def to_screen(longitude: float, latitude: float) -> QPointF:
            return QPointF(
                offset_x + (longitude * frame.longitude_scale - frame.left) * scale,
                offset_y + (-latitude - frame.top) * scale,
            )

        for zone in self.zones:
            path = QPainterPath()
            for ring in ZONE_POLYGONS[zone]:
                path.moveTo(to_screen(*ring[0]))
                for point in ring[1:]:
                    path.lineTo(to_screen(*point))
                path.closeSubpath()
            self._paths[zone] = path
            self._label_points[zone] = to_screen(*ZONE_LABEL_POINTS[zone])
        for west, south, east, north in COUNTRY_INSETS.get(self._country, ()):
            self._inset_rects.append(QRectF(to_screen(west, north), to_screen(east, south)))

    def _ensure_paths(self) -> None:
        if self._paths_for != (self.size(), self._country):
            self._build_paths()

    def zone_at(self, position: QPointF) -> str | None:
        self._ensure_paths()
        for zone in reversed(self._draw_order()):
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
        zones = self.zones
        if step is None or not zones:
            super().keyPressEvent(event)
            return

        if self._selected is None:
            self.set_selected_zone(zones[0])
            return
        index = (zones.index(self._selected) + step) % len(zones)
        self.set_selected_zone(zones[index])

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            zone = self.zone_at(QPointF(event.pos()))  # type: ignore[attr-defined]
            if zone is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(
                    event.globalPos(),  # type: ignore[attr-defined]
                    self._tooltip(zone),
                    self,
                )
            return True
        return super().event(event)

    @staticmethod
    def _tooltip(zone: str) -> str:
        lines = [f"<b>{ZONE_NAMES[zone]}</b> ({zone})"]
        regions = ZONE_REGIONS.get(zone)
        if regions:
            lines.append(regions)
        if zone in MARKER_ZONES:
            lines.append("<i>simbolo: alla scala del paese sarebbe un punto</i>")
        return "<qt>" + "<br>".join(lines) + "</qt>"

    def _fill_color(self, zone: str) -> QColor:
        if zone == self._selected:
            return QColor(self._theme.accent)
        if zone == self._hovered:
            return QColor(self._theme.accent_soft)
        return QColor(self._theme.neutral_soft)

    def _text_color(self, zone: str, inside: bool) -> QColor:
        if zone == self._selected:
            #sopra il riempimento pieno serve il colore della carta; fuori dalla
            #zona, sullo sfondo, sparirebbe
            return QColor(self._theme.card if inside else self._theme.accent)
        if zone == self._hovered:
            return QColor(self._theme.accent)
        return QColor(self._theme.text_muted)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._ensure_paths()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._country is None:
            painter.setPen(_pen(QColor(self._theme.text_muted), 1.0))
            painter.drawText(
                QRectF(self.rect()).adjusted(12.0, 12.0, -12.0, -12.0),
                int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
                self.empty_message(),
            )
        else:
            self._paint_map(painter)

        if self.hasFocus():
            painter.setPen(_pen(QColor(self._theme.accent), 1.0, dashed=True))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(self.rect().adjusted(0, 0, -1, -1)))

        painter.end()

    def _paint_map(self, painter: QPainter) -> None:
        #i riquadri dei territori spostati: dicono che quella posizione non e' vera
        frame_color = QColor(self._theme.text_muted)
        frame_color.setAlpha(110)
        painter.setPen(_pen(frame_color, 1.0, dashed=True))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for rect in self._inset_rects:
            painter.drawRect(rect)

        for zone in self._draw_order():
            #il colore dei bordi del tema, su questi riempimenti, sparisce
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

        if self.width() < _MIN_WIDTH_FOR_LABELS:
            return
        label_font = QFont(self.font())
        label_font.setPointSizeF(8.0)
        painter.setFont(label_font)
        metrics = QFontMetricsF(label_font)
        visible = QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0)

        def priority(zone: str) -> tuple[bool, bool, float]:
            bounds = self._paths[zone].boundingRect()
            return (zone != self._selected, zone != self._hovered, -bounds.width() * bounds.height())

        placed: list[QRectF] = []
        for zone in sorted(self.zones, key=priority):
            name = ZONE_NAMES[zone]
            text_width = metrics.horizontalAdvance(name)
            bounds = self._paths[zone].boundingRect()
            #quasi ci sta: il nome si scrive centrato anche se deborda un po'
            roughly_fits = (
                bounds.width() >= text_width * _LABEL_MIN_WIDTH_RATIO
                and bounds.height() >= metrics.height() * 0.8
            )
            fits = bounds.width() >= text_width + 4.0 and bounds.height() >= metrics.height()
            #il nome della zona scelta o sotto il mouse si scrive sempre
            if not roughly_fits and zone not in (self._selected, self._hovered):
                continue
            inside = fits if zone == self._selected else roughly_fits
            center = self._label_points[zone]
            box = QRectF(
                center.x() - text_width / 2.0 - 2.0,
                center.y() - _LABEL_HEIGHT / 2.0,
                text_width + 4.0,
                _LABEL_HEIGHT,
            )
            if not inside:
                box.moveBottom(bounds.top() - 2.0)
                if box.top() < visible.top():
                    box.moveTop(bounds.bottom() + 2.0)
            #dentro il widget: un nome tagliato dal bordo non si legge
            box.moveLeft(min(max(box.left(), visible.left()), visible.right() - box.width()))
            box.moveTop(min(max(box.top(), visible.top()), visible.bottom() - box.height()))
            if any(box.intersects(other) for other in placed):
                continue
            placed.append(box)
            painter.setPen(_pen(self._text_color(zone, inside), 1.0))
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), name)
