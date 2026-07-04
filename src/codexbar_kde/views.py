"""Views and chart widgets for the CodexBar dashboard.

Design follows CodexBar (codexbar.app): a flat, utilitarian dark surface —
hairline separators, thin meters, teal history bars, and burn-down lines —
rather than heavy card panels.
"""

from __future__ import annotations

import datetime as dt

from PyQt6.QtCore import QRectF, Qt, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .history import BurnDown, SeriesPoint
from .model import ProviderUsage, parse_iso_datetime, severity_for_percent
from .reset import format_expiry, pick_next_expiring

# ---------------------------------------------------------------- palette

BG = "#0b0b0d"
SURFACE = "#111114"
HAIRLINE = "#232329"
TEXT = "#f2f2f4"
MUTED = "#8c8c96"
TEAL = "#4d9aa8"
IDEAL = "#5c5c66"

PROVIDER_ACCENTS = {
    "codex": "#7170ff",
    "claude": "#d97757",
    "gemini": "#5cc8ff",
    "openai": "#10b981",
    "copilot": "#7c8cff",
    "grok": "#e2e2e6",
    "openrouter": "#94a3ff",
}


def provider_accent_color(provider: str) -> str:
    return PROVIDER_ACCENTS.get((provider or "").strip().lower(), "#8a8f98")


def color_for_percent(percent: float) -> str:
    severity = severity_for_percent(percent)
    if severity == "critical":
        return "#ff6b6b"
    if severity == "warn":
        return "#f1c857"
    return "#41d17d"


def progress_style(percent: float, accent: str | None = None) -> str:
    """Thin CodexBar-style meter: 6px track, flat chunk in severity color."""
    color = accent if percent < 70 and accent else color_for_percent(percent)
    return f"""
        QProgressBar {{
            border: none;
            border-radius: 3px;
            background: #26262c;
            max-height: 6px;
            min-height: 6px;
        }}
        QProgressBar::chunk {{
            border-radius: 3px;
            background: {color};
        }}
    """


def hairline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {HAIRLINE}; border: none;")
    return line


def _label(text: str, *, size: int = 13, weight: int = 400, color: str = TEXT) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {color}; font-size: {size}px; font-weight: {weight}; background: transparent;")
    return label


# ---------------------------------------------------------------- charts


class BarChartWidget(QWidget):
    """Daily-peak bar chart in the style of the CodexBar history widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.points: list[SeriesPoint] = []
        self.bar_color = QColor(TEAL)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_points(self, points: list[SeriesPoint], color: str = TEAL) -> None:
        self.points = points
        self.bar_color = QColor(color)
        self.update()

    def paintEvent(self, a0) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(4, 8, -4, -26)
        baseline_y = rect.bottom()
        painter.setPen(QPen(QColor(HAIRLINE), 1))
        painter.drawLine(rect.left(), baseline_y, rect.right(), baseline_y)
        if not self.points:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No history yet — data accrues with each refresh")
            painter.end()
            return
        count = len(self.points)
        slot = rect.width() / count
        bar_w = max(3.0, min(18.0, slot * 0.62))
        label_font = QFont(self.font())
        label_font.setPixelSize(10)
        label_step = max(1, count // 6)
        last_labeled = -10**9
        for index, point in enumerate(self.points):
            x = rect.left() + slot * index + (slot - bar_w) / 2
            h = (max(0.0, min(100.0, point.value)) / 100.0) * rect.height()
            color = QColor(self.bar_color)
            if point.value <= 0:
                color = QColor(HAIRLINE)
                h = 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, baseline_y - h, bar_w, h), 2, 2)
            wants_label = index == 0 or index == count - 1 or (index % label_step == 0 and count - 1 - index >= 3)
            # keep labels at least ~3 slots apart so adjacent dates never collide
            if count <= 31 and wants_label and index - last_labeled >= max(3, label_step // 2):
                painter.setPen(QColor(MUTED))
                painter.setFont(label_font)
                painter.drawText(int(x - slot), baseline_y + 6, int(slot * 3), 14,
                                 Qt.AlignmentFlag.AlignHCenter, point.ts.strftime("%b %d"))
                last_labeled = index
        painter.end()


class BurnDownWidget(QWidget):
    """Remaining-budget line vs ideal steady burn, like the CodexBar widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.burn: BurnDown | None = None
        self.accent = QColor(TEAL)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_burn_down(self, burn: BurnDown | None, color: str = TEAL) -> None:
        self.burn = burn
        self.accent = QColor(color)
        self.update()

    def _x(self, rect, when: dt.datetime) -> float:
        assert self.burn is not None
        total = (self.burn.resets_at - self.burn.window_start).total_seconds()
        frac = 0.0 if total <= 0 else (when - self.burn.window_start).total_seconds() / total
        return rect.left() + max(0.0, min(1.0, frac)) * rect.width()

    def _y(self, rect, remaining: float) -> float:
        return rect.bottom() - (max(0.0, min(100.0, remaining)) / 100.0) * rect.height()

    def paintEvent(self, a0) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 10, -8, -28)
        painter.setPen(QPen(QColor(HAIRLINE), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        burn = self.burn
        if burn is None or not burn.actual:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No samples in the current window yet")
            painter.end()
            return
        # ideal steady-burn dashed line 100% -> 0%
        ideal_pen = QPen(QColor(IDEAL), 1.4)
        ideal_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(ideal_pen)
        painter.drawLine(QPointF(rect.left(), self._y(rect, 100.0)),
                         QPointF(rect.right(), self._y(rect, 0.0)))
        # actual remaining
        pen = QPen(self.accent, 2.0)
        painter.setPen(pen)
        pts = [QPointF(self._x(rect, p.ts), self._y(rect, p.value)) for p in burn.actual]
        for a, b in zip(pts, pts[1:]):
            painter.drawLine(a, b)
        painter.setBrush(self.accent)
        painter.setPen(Qt.PenStyle.NoPen)
        for point in pts:
            painter.drawEllipse(point, 2.6, 2.6)
        # axis captions
        painter.setPen(QColor(MUTED))
        font = QFont(self.font())
        font.setPixelSize(10)
        painter.setFont(font)
        painter.drawText(rect.left(), rect.bottom() + 6, 160, 14, Qt.AlignmentFlag.AlignLeft,
                         burn.window_start.astimezone().strftime("window start %H:%M"))
        painter.drawText(rect.right() - 160, rect.bottom() + 6, 160, 14, Qt.AlignmentFlag.AlignRight,
                         burn.resets_at.astimezone().strftime("reset %H:%M"))
        painter.end()


# ---------------------------------------------------------------- views


class ResetCreditPanel(QFrame):
    """Codex banked reset credits with a one-click redeem for the credit
    that is closest to expiring. Shown only when credits exist."""

    redeem_requested = pyqtSignal(str)  # credit_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ResetPanel")
        self.setStyleSheet(f"""
            QFrame#ResetPanel {{
                background: {SURFACE};
                border: 1px solid {HAIRLINE};
                border-radius: 8px;
            }}
            QPushButton#RedeemButton {{
                background: #2b4c3f;
                color: #d7fbe8;
                border: 1px solid #3f6f5a;
                border-radius: 6px;
                padding: 7px 12px;
                font-weight: 700;
                font-size: 12px;
            }}
            QPushButton#RedeemButton:hover {{ background: #356050; }}
            QPushButton#RedeemButton:disabled {{ background: #20242a; color: {MUTED}; border-color: {HAIRLINE}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(_label("Codex reset credits", size=13, weight=700))
        self.count_label = _label("", size=12, color=MUTED)
        head.addWidget(self.count_label)
        head.addStretch(1)
        self.redeem_button = QPushButton("Redeem")
        self.redeem_button.setObjectName("RedeemButton")
        self.redeem_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redeem_button.clicked.connect(self._emit_redeem)
        head.addWidget(self.redeem_button)
        layout.addLayout(head)
        self.credit_lines = _label("", size=11, color=MUTED)
        self.credit_lines.setWordWrap(True)
        layout.addWidget(self.credit_lines)
        self.status_line = _label("", size=11, color=MUTED)
        self.status_line.setWordWrap(True)
        self.status_line.hide()
        layout.addWidget(self.status_line)
        self._credits: list[dict] = []
        self._target_id: str = ""
        self.hide()

    def credit_count(self) -> int:
        return len(self._credits)

    def target_credit_id(self) -> str:
        return self._target_id

    def set_credits(self, credits: list[dict]) -> None:
        self._credits = [c for c in credits if isinstance(c, dict)]
        available = [c for c in self._credits if c.get("status") == "available"]
        if not available:
            self.hide()
            return
        target = pick_next_expiring(available)
        self._target_id = str(target.get("id") or "") if target else ""
        self.count_label.setText(f"{len(available)} available")
        lines = []
        for credit in sorted(available, key=lambda c: str(c.get("expires_at") or "~")):
            title = str(credit.get("title") or "Reset credit")
            note = format_expiry(credit)
            marker = "→ " if credit.get("id") == self._target_id else "   "
            lines.append(f"{marker}{title}" + (f" · {note}" if note else ""))
        self.credit_lines.setText("\n".join(lines))
        if target:
            note = format_expiry(target)
            self.redeem_button.setText(f"Redeem next ({note})" if note else "Redeem next")
            self.redeem_button.setEnabled(bool(self._target_id))
        self.status_line.hide()
        self.show()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.redeem_button.setEnabled(not busy and bool(self._target_id))
        if message:
            self.status_line.setText(message)
            self.status_line.show()

    def show_result(self, message: str, *, ok: bool) -> None:
        color = "#8fd3a8" if ok else "#ff9b9b"
        self.status_line.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent;")
        self.status_line.setText(message)
        self.status_line.show()

    def _emit_redeem(self) -> None:
        if self._target_id:
            self.redeem_requested.emit(self._target_id)


class OverviewView(QWidget):
    """Flat CodexBar-menu style listing: provider sections with thin meters."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        self.reset_panel = ResetCreditPanel()
        outer.addWidget(self.reset_panel)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        outer.addWidget(self.scroll_area)
        self._host = QWidget()
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(4, 4, 12, 12)
        self._layout.setSpacing(0)
        self.scroll_area.setWidget(self._host)
        self._summary_parts: list[str] = []

    def summary_text(self) -> str:
        return "\n".join(self._summary_parts)

    def set_reset_credits(self, credits: list[dict]) -> None:
        self.reset_panel.set_credits(credits)

    def set_providers(self, providers: list[ProviderUsage]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._summary_parts = []
        if not providers:
            self._layout.addWidget(_label("No provider data yet — refresh or enable providers in the CodexBar CLI.", color=MUTED))
            self._layout.addStretch(1)
            return
        for index, provider in enumerate(providers):
            if index:
                self._layout.addSpacing(18)
            self._layout.addWidget(self._section(provider))
        self._layout.addStretch(1)

    def _section(self, provider: ProviderUsage) -> QWidget:
        accent = provider_accent_color(provider.provider)
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {'#ff6b6b' if provider.error else accent}; font-size: 11px; background: transparent;")
        head.addWidget(dot)
        title = _label(provider.display_name, size=16, weight=700)
        head.addWidget(title)
        meta_bits = [bit for bit in (provider.source, f"v{provider.version}" if provider.version else "") if bit]
        if meta_bits:
            head.addWidget(_label(" · ".join(meta_bits), size=11, color=MUTED))
        head.addStretch(1)
        if not provider.error and provider.windows:
            peak = provider.max_used_percent
            head.addWidget(_label(f"{100 - peak:.0f}% left", size=13, weight=700,
                                  color=color_for_percent(peak)))
        layout.addLayout(head)
        layout.addWidget(hairline())
        self._summary_parts.append(provider.display_name)

        if provider.error:
            err = _label(f"Error: {provider.error}", size=12, color="#ff9b9b")
            err.setWordWrap(True)
            layout.addWidget(err)
            self._summary_parts.append(provider.error)
            return box

        for window in provider.windows:
            layout.addLayout(self._window_row(window, accent))
        footer_bits = []
        if provider.credits_remaining is not None:
            footer_bits.append(f"Credits {provider.credits_remaining:g}")
        if footer_bits:
            layout.addWidget(_label("   ·   ".join(footer_bits), size=11, color=MUTED))
        return box

    def _window_row(self, window, accent: str) -> QVBoxLayout:
        row = QVBoxLayout()
        row.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(_label(window.label, size=12, weight=600))
        top.addStretch(1)
        left = 100 - window.used_percent
        value_bits = [f"{left:.0f}% left"]
        if window.reset_countdown:
            value_bits.append(f"resets in {window.reset_countdown}")
        top.addWidget(_label("  ·  ".join(value_bits), size=12, color=MUTED))
        row.addLayout(top)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(round(window.used_percent)))
        bar.setTextVisible(False)
        bar.setStyleSheet(progress_style(window.used_percent, accent))
        row.addWidget(bar)
        if window.pace_note:
            row.addWidget(_label(window.pace_note, size=11, color=MUTED))
        return row


class HistoryView(QWidget):
    """Daily peak-usage bar chart with a provider/window selector."""

    selection_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 12, 12)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(lambda _: self.selection_changed.emit())
        controls.addWidget(self.selector)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.caption = _label("Peak usage per day · last 30 days", size=11, color=MUTED)
        layout.addWidget(self.caption)
        self.chart = BarChartWidget()
        layout.addWidget(self.chart, 1)
        self.summary = _label("", size=12, color=MUTED)
        layout.addWidget(self.summary)

    def set_options(self, options: list[tuple[str, str, str]]) -> None:
        """options: (provider_key, window_key, label)."""
        current = self.selector.currentData()
        self.selector.blockSignals(True)
        self.selector.clear()
        for provider_key, window_key, label in options:
            self.selector.addItem(label, (provider_key, window_key))
        if current is not None:
            index = self.selector.findData(current)
            if index >= 0:
                self.selector.setCurrentIndex(index)
        self.selector.blockSignals(False)

    def current_selection(self) -> tuple[str, str] | None:
        return self.selector.currentData()

    def set_series(self, points: list[SeriesPoint], accent: str, summary: str) -> None:
        self.chart.set_points(points, accent)
        self.summary.setText(summary)


class BurnDownView(QWidget):
    """Remaining budget vs ideal steady burn for the active window."""

    selection_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 12, 12)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(lambda _: self.selection_changed.emit())
        controls.addWidget(self.selector)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.caption = _label("Remaining budget vs ideal steady burn (dashed)", size=11, color=MUTED)
        layout.addWidget(self.caption)
        self.chart = BurnDownWidget()
        layout.addWidget(self.chart, 1)
        self.summary = _label("", size=12, color=MUTED)
        layout.addWidget(self.summary)

    def set_options(self, options: list[tuple[str, str, str]]) -> None:
        current = self.selector.currentData()
        self.selector.blockSignals(True)
        self.selector.clear()
        for provider_key, window_key, label in options:
            self.selector.addItem(label, (provider_key, window_key))
        if current is not None:
            index = self.selector.findData(current)
            if index >= 0:
                self.selector.setCurrentIndex(index)
        self.selector.blockSignals(False)

    def current_selection(self) -> tuple[str, str] | None:
        return self.selector.currentData()

    def set_burn_down(self, burn: BurnDown | None, accent: str, summary: str) -> None:
        self.chart.set_burn_down(burn, accent)
        self.summary.setText(summary)


class DetailsView(QWidget):
    """Read-only monospace dump of the compact per-provider fields."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(_label("Everything meaningful from `codexbar usage`, compacted.", size=11, color=MUTED))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPixelSize(12)
        self.text.setFont(font)
        self.text.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {SURFACE};
                color: {TEXT};
                border: 1px solid {HAIRLINE};
                border-radius: 6px;
                padding: 10px;
            }}
        """)
        layout.addWidget(self.text, 1)

    def set_text(self, value: str) -> None:
        self.text.setPlainText(value)

    def plain_text(self) -> str:
        return self.text.toPlainText()


def window_options(providers: list[ProviderUsage]) -> list[tuple[str, str, str]]:
    """Selector entries for history/burn-down: one per provider window."""
    options: list[tuple[str, str, str]] = []
    for provider in providers:
        if provider.error:
            continue
        for window in provider.windows:
            options.append((provider.provider, window.key, f"{provider.display_name} — {window.label}"))
    return options


def latest_reset_at(providers: list[ProviderUsage], provider_key: str, window_key: str) -> tuple[dt.datetime | None, int | None]:
    for provider in providers:
        if provider.provider != provider_key:
            continue
        for window in provider.windows:
            if window.key == window_key:
                return parse_iso_datetime(window.resets_at), window.window_minutes
    return None, None
