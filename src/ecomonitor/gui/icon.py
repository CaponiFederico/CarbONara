from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

#Icona dell'applicazione, usata dalla finestra e dall'area di notifica.

_ICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="7" fill="#2e7d32"/>
  <path d="M7 25 C7 13.5 13.5 7 25 7 C25 18.5 18.5 25 7 25 Z" fill="#ffffff"/>
  <path d="M8.5 24 L20 12.5" stroke="#2e7d32" stroke-width="1.6" stroke-linecap="round"/>
</svg>"""

#Le dimensioni 
_ICON_SIZES = (16, 24, 32, 48, 256)

_cached_icon: QIcon | None = None


def _render(size: int) -> QPixmap:
    #un renderer per dimensione
    renderer = QSvgRenderer(QByteArray(_ICON_SVG))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()  # senza, il QPainter resta attivo sulla pixmap
    return pixmap


def app_icon() -> QIcon:
    #Va chiamata dopo la creazione della QApplication
    global _cached_icon
    if _cached_icon is None:
        icon = QIcon()
        for size in _ICON_SIZES:
            icon.addPixmap(_render(size))
        _cached_icon = icon
    return _cached_icon
