#Genera i file d'icona che servono agli installatori
#   python packaging/make_icon.py
#
#packaging/ecomonitor.ico (Windows) e packaging/ecomonitor.png (Linux).

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from ecomonitor.gui.icon import _ICON_SVG

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_SIZE = 256


def render_png(size: int) -> bytes:
    #un renderer per dimensione
    renderer = QSvgRenderer(QByteArray(_ICON_SVG))
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)  # trasparente
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()  # senza, il QPainter resta attivo sull'immagine

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"Qt non ha saputo codificare il PNG a {size}px")
    return bytes(buffer.data())


def build_ico(frames: dict[int, bytes]) -> bytes:
    #ICO con PNG incapsulati
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    directory = b""
    payload = b""
    offset = len(header) + 16 * count
    for size, png in sorted(frames.items()):
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  
            size if size < 256 else 0,
            0,  # nessuna tavolozza
            0,  # riservato
            1,  # piani
            32,  # bit per pixel
            len(png),
            offset,
        )
        payload += png
        offset += len(png)
    return header + directory + payload


def main() -> int:
    #QGuiApplication deve esistere prima di qualunque QImage/QPainter
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    assert app is not None

    out_dir = Path(__file__).resolve().parent
    frames = {size: render_png(size) for size in ICO_SIZES}

    ico_path = out_dir / "ecomonitor.ico"
    ico_path.write_bytes(build_ico(frames))
    png_path = out_dir / "ecomonitor.png"
    png_path.write_bytes(frames[PNG_SIZE])

    print(f"scritto {ico_path} ({ico_path.stat().st_size} byte, {len(frames)} dimensioni)")
    print(f"scritto {png_path} ({png_path.stat().st_size} byte, {PNG_SIZE}px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
