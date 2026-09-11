"""Generate the application icon as a real .ico file.

The icon is drawn with the same palette and the same "bb" wordmark the app shows
in its top bar, so the executable, the taskbar entry and the window all match.
Generating it keeps a binary asset out of the repository while still producing a
proper multi-resolution icon.

An .ico is assembled by hand rather than with Pillow: the format allows PNG
payloads, so the container is a 6-byte header plus one 16-byte directory entry
per size - not worth a new dependency.

Usage:  python packaging/make_icon.py [output.ico]
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bbpull.palette import DARK  # noqa: E402

#: Windows picks the closest size; 256 is used by large-icon views.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size):
    """One square icon: rounded accent tile with the 'bb' wordmark."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    painter.setPen(Qt.NoPen)

    accent = QColor(DARK["accent"])
    margin = size * 0.06
    radius = size * 0.22
    painter.setBrush(accent)
    painter.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin), radius, radius)

    # At 16px the lettering is illegible; a solid tile reads better.
    if size >= 24:
        font = QFont("Microsoft JhengHei UI")
        font.setPixelSize(max(7, int(size * 0.46)))
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(QRectF(0, -size * 0.02, size, size),
                         Qt.AlignCenter, "bb")

    painter.end()
    return pixmap


def png_bytes(pixmap):
    """Encode a pixmap to PNG bytes.

    The QByteArray must outlive the QBuffer: passing a temporary
    (`QBuffer(QByteArray())`) lets Python collect the storage while Qt still
    writes into it, which crashes the process with an access violation rather
    than raising.
    """
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QBuffer.WriteOnly)
    pixmap.save(buffer, "PNG")
    buffer.close()
    return bytes(storage)


def write_ico(path, sizes=SIZES):
    """Assemble a multi-resolution .ico containing PNG payloads."""
    payloads = []
    app = QApplication.instance() or QApplication([])
    assert app is not None

    for size in sizes:
        payloads.append((size, png_bytes(render(size))))

    header = struct.pack("<HHH", 0, 1, len(payloads))       # reserved, type=icon, count
    offset = len(header) + 16 * len(payloads)
    entries = b""
    for size, data in payloads:
        dimension = 0 if size >= 256 else size              # 0 means 256 in the format
        entries += struct.pack(
            "<BBBBHHII",
            dimension, dimension, 0, 0,                     # w, h, colours, reserved
            1, 32,                                          # planes, bit depth
            len(data), offset,
        )
        offset += len(data)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(entries)
        for _size, data in payloads:
            handle.write(data)
    return path, len(payloads)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    target = argv[0] if argv else "packaging/bbpull.ico"
    path, count = write_ico(target)
    print(f"wrote {path} ({count} sizes, {path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
