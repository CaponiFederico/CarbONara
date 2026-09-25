#Genera i file d'icona che servono agli installatori
#   python packaging/make_icon.py
#
#packaging/ecomonitor.ico (Windows), packaging/ecomonitor.png (Linux) e
#packaging/ecomonitor.icns (macOS), senza iconutil

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

ICNS_TYPES = (
    #icp4/icp5: 16 e 32 px a 1x, senza Finder rimpicciolisce le grandi
    (b"icp4", 16),
    (b"icp5", 32),
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic07", 128),
    (b"ic13", 256),
    (b"ic08", 256),
    (b"ic14", 512),
    (b"ic09", 512),
    (b"ic10", 1024),
)


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


def build_icns(frames: dict[int, bytes]) -> bytes:
    entries = b"".join(
        icon_type + struct.pack(">I", 8 + len(frames[size])) + frames[size]
        for icon_type, size in ICNS_TYPES
    )
    return b"icns" + struct.pack(">I", 8 + len(entries)) + entries


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

    icns_frames = {size: render_png(size) for _, size in ICNS_TYPES}
    icns_path = out_dir / "ecomonitor.icns"
    icns_path.write_bytes(build_icns(icns_frames))

    print(f"scritto {ico_path} ({ico_path.stat().st_size} byte, {len(frames)} dimensioni)")
    print(f"scritto {png_path} ({png_path.stat().st_size} byte, {PNG_SIZE}px)")
    print(f"scritto {icns_path} ({icns_path.stat().st_size} byte, {len(ICNS_TYPES)} voci)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
