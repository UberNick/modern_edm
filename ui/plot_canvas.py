"""
Live 2D Plan (X-Y) & Cross-Section (X-Z / Y-Z) Canvas Widget.
Draws active excavation unit boundaries, known datums, station position, and recorded shots.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from survey.math_engine import Datum, ExcavationUnit, StationState


class SurveyPlotCanvas(QWidget):
    """High-contrast 2D field plot widget for verifying shots in real time."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(380, 320)
        self.view_mode: str = "XY"
        self.points: List[Dict[str, Any]] = []
        self.datums: List[Datum] = []
        self.units: List[ExcavationUnit] = []
        self.station: Optional[StationState] = None

    def set_data(
        self,
        points: List[Dict[str, Any]],
        datums: List[Datum],
        units: List[ExcavationUnit],
        station: StationState,
    ) -> None:
        self.points = points
        self.datums = datums
        self.units = units
        self.station = station
        self.update()

    def set_view_mode(self, mode: str) -> None:
        self.view_mode = mode.upper()
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()
        painter.fillRect(rect, QColor("#14191F"))

        xs: List[float] = []
        ys: List[float] = []

        def extract_coords(x: float, y: float, z: float) -> tuple[float, float]:
            if self.view_mode == "XZ":
                return x, z
            if self.view_mode == "YZ":
                return y, z
            return x, y

        for u in self.units:
            if self.view_mode == "XY":
                if u.shape.upper() == "CIRCLE":
                    xs.extend([u.center_x - u.radius, u.center_x + u.radius])
                    ys.extend([u.center_y - u.radius, u.center_y + u.radius])
                else:
                    xs.extend([u.min_x, u.max_x])
                    ys.extend([u.min_y, u.max_y])

        for d in self.datums:
            cx, cy = extract_coords(d.x, d.y, d.z)
            xs.append(cx)
            ys.append(cy)

        for p in self.points:
            cx, cy = extract_coords(p["x"], p["y"], p["z"])
            xs.append(cx)
            ys.append(cy)

        if self.station:
            cx, cy = extract_coords(self.station.x, self.station.y, self.station.z)
            xs.append(cx)
            ys.append(cy)

        if not xs or not ys:
            xs, ys = [99.0, 103.0], [99.0, 103.0]

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)
        pad = 0.15 * max(span_x, span_y)
        min_x -= pad
        max_x += pad
        min_y -= pad
        max_y += pad

        w = rect.width() - 40
        h = rect.height() - 40
        scale = min(w / (max_x - min_x), h / (max_y - min_y))

        def to_screen(wx: float, wy: float) -> tuple[int, int]:
            sx = int(20 + (wx - min_x) * scale)
            sy = int(rect.height() - 20 - (wy - min_y) * scale)
            return sx, sy

        if self.view_mode == "XY":
            unit_pen = QPen(QColor("#2EC4B6"), 2, Qt.DashLine)
            painter.setPen(unit_pen)
            for u in self.units:
                if u.shape.upper() == "CIRCLE":
                    cx, cy = to_screen(u.center_x, u.center_y)
                    r_px = int(u.radius * scale)
                    painter.drawEllipse(cx - r_px, cy - r_px, 2 * r_px, 2 * r_px)
                    painter.drawText(cx - r_px, cy - r_px - 4, u.name)
                else:
                    x1, y1 = to_screen(u.min_x, u.max_y)
                    x2, y2 = to_screen(u.max_x, u.min_y)
                    painter.drawRect(x1, y1, x2 - x1, y2 - y1)
                    painter.drawText(x1 + 4, y1 + 14, u.name)

        datum_pen = QPen(QColor("#FF9F1C"), 2)
        painter.setPen(datum_pen)
        for d in self.datums:
            dx, dy = extract_coords(d.x, d.y, d.z)
            sx, sy = to_screen(dx, dy)
            painter.drawLine(sx - 6, sy, sx + 6, sy)
            painter.drawLine(sx, sy - 6, sx, sy + 6)
            painter.drawText(sx + 6, sy - 4, f"▲{d.name}")

        if self.station:
            stx, sty = extract_coords(self.station.x, self.station.y, self.station.z)
            sx, sy = to_screen(stx, sty)
            painter.setPen(QPen(QColor("#00BBF9"), 2))
            painter.drawRect(sx - 5, sy - 5, 10, 10)
            painter.drawText(sx + 8, sy + 4, f"STN ({self.station.station_name})")

        for idx, p in enumerate(self.points):
            px, py = extract_coords(p["x"], p["y"], p["z"])
            sx, sy = to_screen(px, py)
            is_last = idx == len(self.points) - 1
            color = QColor("#FF595E") if is_last else QColor("#8AC926")
            radius = 5 if is_last else 3
            painter.setPen(QPen(color, 2))
            painter.setBrush(color)
            painter.drawEllipse(sx - radius, sy - radius, radius * 2, radius * 2)
            if is_last:
                painter.drawText(sx + 7, sy - 4, f"#{p['id']}.{p['suffix']}")

        painter.setPen(QPen(QColor("#ADB5BD"), 1))
        painter.setFont(QFont("Consolas", 9))
        painter.drawText(10, 18, f"View: {self.view_mode} | Shots: {len(self.points)}")
