"""Views and chart widgets for the CodexBar dashboard.

Design follows CodexBar (codexbar.app): a flat, utilitarian dark surface —
hairline separators, thin meters, teal history bars, and burn-down lines —
rather than heavy card panels.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:  # QtSvg ships with the PyQt6 wheel, but stay usable without it.
    from PyQt6.QtSvg import QSvgRenderer as _QSvgRenderer

    _svg_renderer: type[_QSvgRenderer] | None = _QSvgRenderer
except ImportError:  # pragma: no cover - depends on install flavor
    _svg_renderer = None

from .history import BurnDown, SeriesPoint
from .model import (
    ProviderUsage,
    WindowUsage,
    fleet_next_reset,
    fleet_tightest,
    parse_iso_datetime,
    severity_for_percent,
)
from .privacy import redact_text
from .reset import available_reset_credits, format_expiry, pick_next_expiring

# ---------------------------------------------------------------- palette

BG = "#0b0b0d"
SURFACE = "#111114"
HAIRLINE = "#232329"
TEXT = "#f2f2f4"
MUTED = "#8c8c96"
TEAL = "#4d9aa8"
IDEAL = "#5c5c66"
GOOD = "#41d17d"
WARN = "#f1c857"
CRIT = "#ff6b6b"
SOFT_GOOD = "#8fd3a8"
SOFT_CRIT = "#ff9b9b"
TRACK = "#26262c"
_TRACK_COLOR = QColor(TRACK)

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


# Provider brand icons vendored from CodexBar (steipete/CodexBar, MIT),
# recolored for dark surfaces. Keyed by codexbar provider identifier.
ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"
_ICON_ALIASES = {
    "openai": "codex",
    "azureopenai": "codex",
    "azure-openai": "codex",
    "groqcloud": "groq",
    "abacusai": "abacus",
    "kimik2": "kimi",
    "moonshot": "kimi",
    "alibaba-coding-plan": "alibaba",
    "alibaba-token-plan": "alibaba",
}
_ICON_CACHE: dict[tuple[str, int], QPixmap] = {}


def provider_icon_path(provider: str) -> Path | None:
    key = (provider or "").strip().lower()
    key = _ICON_ALIASES.get(key, key)
    path = ICON_DIR / f"ProviderIcon-{key}.svg"
    return path if path.is_file() else None


# Brand marks that keep their original white glyph instead of the accent
# tint — OpenAI's mark is white by brand, and purple made it read as generic.
UNTINTED_BADGES = {"codex", "openai"}


def provider_badge(provider: str, size: int = 24) -> QPixmap:
    """Brand icon rendered from SVG tinted in the provider accent, or an
    accent-colored initial disc. The vendored icons are white monochrome, so
    a SourceIn fill recolors exactly the drawn glyph."""
    key = (provider or "").strip().lower()
    cached = _ICON_CACHE.get((key, size))
    if cached is not None:
        return cached
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = provider_icon_path(key)
    if path is not None and _svg_renderer is not None:
        _svg_renderer(str(path)).render(painter, QRectF(0, 0, size * 2, size * 2))
        if key not in UNTINTED_BADGES:
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceIn
            )
            painter.fillRect(pixmap.rect(), QColor(provider_accent_color(key)))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(provider_accent_color(key)))
        painter.drawEllipse(0, 0, size * 2, size * 2)
        painter.setPen(QColor(BG))
        font = QFont()
        font.setPixelSize(size)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, 0, size * 2, size * 2),
            Qt.AlignmentFlag.AlignCenter,
            (key[:1] or "?").upper(),
        )
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    _ICON_CACHE[(key, size)] = pixmap
    return pixmap


def color_for_percent(percent: float) -> str:
    severity = severity_for_percent(percent)
    if severity == "critical":
        return CRIT
    if severity == "warn":
        return WARN
    return GOOD


def meter_color(percent_used: float, accent: str | None = None) -> str:
    """The Overview meter rule, shared by every usage renderer: provider
    accent while healthy (<70% used), severity amber/red once hot."""
    if percent_used < 70 and accent:
        return accent
    return color_for_percent(percent_used)


def hairline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {HAIRLINE}; border: none;")
    return line


@lru_cache(maxsize=1)
def _row_metrics() -> QFontMetrics:
    """Metrics matching _label(size=12)'s rendered font — a bare QLabel's
    font() is unpolished (the stylesheet's px size not yet applied), so
    measure a QFont with the pixel size set explicitly instead."""
    font = QFont()
    font.setPixelSize(12)
    return QFontMetrics(font)


def _clear_layout(layout: QLayout, *, keep_tail: int = 0) -> None:
    """Delete every widget, recursing into nested layouts — items added with
    addLayout() keep their child widgets parented to the host, so a
    widget-only sweep leaves them painting over the rebuilt content.
    ``keep_tail`` preserves trailing items (e.g. a stretch)."""
    while layout.count() > keep_tail:
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child is not None:
            _clear_layout(child)
            child.deleteLater()


def _label(
    text: str, *, size: int = 13, weight: int = 400, color: str = TEXT
) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight}; background: transparent;"
    )
    return label


def _caption(text: str) -> QLabel:
    """Uppercase micro-caption, same voice as the aggregate strip headers."""
    return _label(text.upper(), size=10, weight=600, color=MUTED)


def _mono(text: str, color: str = TEXT, *, weight: int = 400) -> QLabel:
    """Selectable monospace label for the compact codexbar field dumps."""
    label = QLabel(text)
    font = QFont("monospace")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(12)
    label.setFont(font)
    label.setStyleSheet(
        f"color: {color}; font-weight: {weight}; background: transparent;"
    )
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _card() -> tuple[QFrame, QVBoxLayout]:
    """Surface panel with hairline border — the Overview panel look."""
    frame = QFrame()
    frame.setObjectName("Card")
    frame.setStyleSheet(
        f"QFrame#Card {{ background: {SURFACE}; border: 1px solid {HAIRLINE};"
        f" border-radius: 8px; }}"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    return frame, layout


def _fill_selector(selector: QComboBox, options: list[tuple[str, str, str]]) -> None:
    """(Re)populate a provider/window selector with brand badges, keeping the
    current selection when it survives the refresh. Matches by Python
    equality — QComboBox.findData() compares wrapped tuples by identity and
    would silently reset the selection on every refresh."""
    current = selector.currentData()
    selector.blockSignals(True)
    selector.clear()
    for index, (provider_key, window_key, label) in enumerate(options):
        selector.addItem(
            QIcon(provider_badge(provider_key, 16)),
            label,
            (provider_key, window_key),
        )
        if (provider_key, window_key) == current:
            selector.setCurrentIndex(index)
    selector.blockSignals(False)


# ---------------------------------------------------------------- charts


class BarChartWidget(QWidget):
    """Daily-peak bar chart in the style of the CodexBar history widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.points: list[SeriesPoint] = []
        self.accent = TEAL
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_points(self, points: list[SeriesPoint], color: str = TEAL) -> None:
        self.points = points
        self.accent = color
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
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No history yet — data accrues with each refresh",
            )
            painter.end()
            return
        count = len(self.points)
        slot = rect.width() / count
        bar_w = max(3.0, min(18.0, slot * 0.62))
        label_font = QFont(self.font())
        label_font.setPixelSize(10)
        label_step = max(1, count // 6)
        last_labeled = -(10**9)
        for index, point in enumerate(self.points):
            x = rect.left() + slot * index + (slot - bar_w) / 2
            h = (max(0.0, min(100.0, point.value)) / 100.0) * rect.height()
            color = QColor(meter_color(point.value, self.accent))
            if point.value <= 0:
                color = QColor(HAIRLINE)
                h = 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, baseline_y - h, bar_w, h), 2, 2)
            wants_label = (
                index == 0
                or index == count - 1
                or (index % label_step == 0 and count - 1 - index >= 3)
            )
            # keep labels at least ~3 slots apart so adjacent dates never collide
            if (
                count <= 31
                and wants_label
                and index - last_labeled >= max(3, label_step // 2)
            ):
                painter.setPen(QColor(MUTED))
                painter.setFont(label_font)
                painter.drawText(
                    int(x - slot),
                    baseline_y + 6,
                    int(slot * 3),
                    14,
                    Qt.AlignmentFlag.AlignHCenter,
                    point.ts.strftime("%b %d"),
                )
                last_labeled = index
        painter.end()


class BurnDownWidget(QWidget):
    """Remaining-budget line vs ideal steady burn, like the CodexBar widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.burn: BurnDown | None = None
        self.accent = TEAL
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_burn_down(self, burn: BurnDown | None, color: str = TEAL) -> None:
        self.burn = burn
        self.accent = color
        self.update()

    def _x(self, rect, burn: BurnDown, when: dt.datetime) -> float:
        total = (burn.resets_at - burn.window_start).total_seconds()
        frac = 0.0 if total <= 0 else (when - burn.window_start).total_seconds() / total
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
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No samples in the current window yet",
            )
            painter.end()
            return
        # ideal steady-burn dashed line 100% -> 0%
        ideal_pen = QPen(QColor(IDEAL), 1.4)
        ideal_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(ideal_pen)
        painter.drawLine(
            QPointF(rect.left(), self._y(rect, 100.0)),
            QPointF(rect.right(), self._y(rect, 0.0)),
        )
        # actual remaining, colored by the meter rule for current usage
        line_color = QColor(meter_color(100 - burn.actual[-1].value, self.accent))
        painter.setPen(QPen(line_color, 2.0))
        pts = [
            QPointF(self._x(rect, burn, p.ts), self._y(rect, p.value))
            for p in burn.actual
        ]
        for a, b in zip(pts, pts[1:]):
            painter.drawLine(a, b)
        painter.setBrush(line_color)
        painter.setPen(Qt.PenStyle.NoPen)
        for point in pts:
            painter.drawEllipse(point, 2.6, 2.6)
        # axis captions
        painter.setPen(QColor(MUTED))
        font = QFont(self.font())
        font.setPixelSize(10)
        painter.setFont(font)
        painter.drawText(
            rect.left(),
            rect.bottom() + 6,
            160,
            14,
            Qt.AlignmentFlag.AlignLeft,
            burn.window_start.astimezone().strftime("window start %H:%M"),
        )
        painter.drawText(
            rect.right() - 160,
            rect.bottom() + 6,
            160,
            14,
            Qt.AlignmentFlag.AlignRight,
            burn.resets_at.astimezone().strftime("reset %H:%M"),
        )
        painter.end()


# ---------------------------------------------------------------- meters


class SegmentBar(QWidget):
    """Segmented block meter: lit blocks show budget remaining, and a hollow
    outlined block marks where the budget should be at linear pace."""

    BLOCKS = 36

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.used_percent = 0.0
        self.expected_used: float | None = None
        self.accent: str | None = None
        self._fill = QColor(meter_color(0.0))
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(
        self,
        used_percent: float,
        expected_used: float | None,
        accent: str | None = None,
    ) -> None:
        self.used_percent = max(0.0, min(100.0, used_percent))
        self.expected_used = expected_used
        self.accent = accent
        self._fill = QColor(meter_color(self.used_percent, accent))
        if expected_used is not None:
            self.setToolTip(
                f"Hollow block: expected {expected_used:g}% used at linear pace"
            )
        else:
            self.setToolTip("")
        self.update()

    def paintEvent(self, a0) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        width, height = self.width(), self.height()
        gap = 2.0
        count = self.BLOCKS
        block_w = (width - gap * (count - 1)) / count
        if block_w < 2.0:
            count = max(8, int(width // 5))
            block_w = (width - gap * (count - 1)) / count
        remaining = 100.0 - self.used_percent
        lit = round(remaining / 100.0 * count)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(count):
            x = index * (block_w + gap)
            painter.setBrush(self._fill if index < lit else _TRACK_COLOR)
            painter.drawRect(QRectF(x, 0, block_w, height))
        if self.expected_used is not None:
            expected_left = 100.0 - self.expected_used
            marker = min(count - 1, max(0, round(expected_left / 100.0 * count) - 1))
            painter.setPen(QPen(QColor(TEXT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            x = marker * (block_w + gap)
            painter.drawRect(QRectF(x + 0.5, 0.5, block_w - 1.0, height - 1.0))
        painter.end()


class SparkBar(QWidget):
    """3px continuous meter for rail rows."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.used_percent = 0.0
        self.accent: str | None = None
        self._fill = QColor(meter_color(0.0))
        self.setFixedHeight(3)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, used_percent: float, accent: str | None = None) -> None:
        self.used_percent = max(0.0, min(100.0, used_percent))
        self.accent = accent
        self._fill = QColor(meter_color(self.used_percent, accent))
        self.update()

    def paintEvent(self, a0) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_TRACK_COLOR)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), 3), 1.5, 1.5)
        remaining = (100.0 - self.used_percent) / 100.0 * self.width()
        painter.setBrush(self._fill)
        painter.drawRoundedRect(QRectF(0, 0, remaining, 3), 1.5, 1.5)
        painter.end()


# ---------------------------------------------------------------- views


class StatStrip(QFrame):
    """Row of caption-over-value stat cells under a hairline — the signature
    header used by every view."""

    VALUE_SIZE = 15
    VALUE_WEIGHT = 700

    def __init__(
        self, cells: list[tuple[str, str]], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatStrip")
        self.setStyleSheet(
            f"QFrame#StatStrip {{ border-bottom: 1px solid {HAIRLINE}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 12)
        layout.setSpacing(0)
        self._cells: dict[str, QLabel] = {}
        for index, (key, caption) in enumerate(cells):
            if index:
                # space-between: cells spread across the full strip width
                layout.addStretch(1)
            cell = QVBoxLayout()
            cell.setSpacing(1)
            cell.addWidget(_caption(caption))
            value = _label("—", size=self.VALUE_SIZE, weight=self.VALUE_WEIGHT)
            cell.addWidget(value)
            self._cells[key] = value
            layout.addLayout(cell)

    def set(self, key: str, text: str, color: str = TEXT) -> None:
        self._cells[key].setText(text)
        self._cells[key].setStyleSheet(
            f"color: {color}; font-size: {self.VALUE_SIZE}px;"
            f" font-weight: {self.VALUE_WEIGHT}; background: transparent;"
        )


class AggregateStrip(StatStrip):
    """Fleet-level answers above the rail: tightest window, next reset,
    live provider count. (Codex reset credits live in ResetCreditPanel —
    they're provider-specific, not fleet-level.)"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            [
                ("tightest", "Tightest window"),
                ("reset", "Next reset"),
                ("live", "Providers"),
            ],
            parent,
        )

    def set_data(
        self,
        providers: list[ProviderUsage],
        *,
        privacy_mode: bool = True,
    ) -> None:
        live = [p for p in providers if not p.error and p.windows]
        errors = sum(1 for p in providers if p.error)

        tightest = fleet_tightest(live)
        if tightest:
            provider, window = tightest
            left = 100 - window.used_percent
            self.set(
                "tightest",
                f"{left:.0f}% · {provider.display_name} "
                f"{redact_text(window.label, redact_emails=privacy_mode)}",
                color_for_percent(window.used_percent),
            )
        else:
            self.set("tightest", "—", MUTED)

        soonest = fleet_next_reset(live)
        if soonest:
            _, provider, window = soonest
            when = window.reset_countdown or window.reset_description or "soon"
            self.set("reset", f"{when} · {provider.display_name}")
        else:
            self.set("reset", "—", MUTED)

        live_text = f"● {len(live)} live"
        if errors:
            self.set("live", f"{live_text} · {errors} error", WARN)
        else:
            self.set("live", live_text, GOOD if live else MUTED)


class ProviderRailItem(QFrame):
    """One clickable provider row: badge, name, tightest countdown, % left."""

    clicked = pyqtSignal(str)

    def __init__(self, provider: ProviderUsage, *, safe_text, parent=None) -> None:
        super().__init__(parent)
        self.provider_key = provider.provider
        self.setObjectName("RailItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        badge = QLabel()
        badge.setPixmap(provider_badge(provider.provider, 20))
        badge.setStyleSheet("background: transparent;")
        top.addWidget(badge)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(_label(safe_text(provider.display_name), size=13, weight=600))
        sub_text = self._sub_text(provider, safe_text)
        if sub_text:
            names.addWidget(_label(sub_text, size=10, color=MUTED))
        top.addLayout(names)
        top.addStretch(1)
        if provider.error:
            top.addWidget(_label("!", size=13, weight=700, color=WARN))
        elif provider.windows:
            peak = provider.max_used_percent
            top.addWidget(
                _label(
                    f"{100 - peak:.0f}%",
                    size=13,
                    weight=700,
                    color=color_for_percent(peak),
                )
            )
        layout.addLayout(top)
        if not provider.error and provider.windows:
            spark = SparkBar()
            spark.set_value(
                provider.max_used_percent, provider_accent_color(provider.provider)
            )
            layout.addWidget(spark)

    @staticmethod
    def _sub_text(provider: ProviderUsage, safe_text) -> str:
        if provider.error:
            return "error"
        tightest = provider.tightest_window
        if tightest is None:
            return ""
        bits = [safe_text(tightest.label)]
        if tightest.reset_countdown:
            bits.append(tightest.reset_countdown)
        return " · ".join(bits)

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet(
            f"""
            QFrame#RailItem {{
                background: {"#1b1b22" if selected else "transparent"};
                border: none;
                border-left: 2px solid {"#7aa2f7" if selected else "transparent"};
                border-radius: 6px;
            }}
            QFrame#RailItem:hover {{ background: #1b1b22; }}
            """
        )

    def mousePressEvent(self, a0) -> None:  # noqa: N802 (Qt override)
        self.clicked.emit(self.provider_key)
        super().mousePressEvent(a0)


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
        # one row: info column left, redeem button right — no dead space
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)
        info = QVBoxLayout()
        info.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_label("Codex reset credits", size=13, weight=700))
        self.count_label = _label("", size=12, color=MUTED)
        head.addWidget(self.count_label)
        head.addStretch(1)
        info.addLayout(head)
        self.credit_lines = _label("", size=11, color=MUTED)
        self.credit_lines.setWordWrap(True)
        info.addWidget(self.credit_lines)
        layout.addLayout(info, 1)
        self.redeem_button = QPushButton("Redeem")
        self.redeem_button.setObjectName("RedeemButton")
        self.redeem_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redeem_button.clicked.connect(self._emit_redeem)
        layout.addWidget(self.redeem_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_line = _label("", size=11, color=MUTED)
        self.status_line.setWordWrap(True)
        self.status_line.hide()
        info.addWidget(self.status_line)
        self._credits: list[dict] = []
        self._target_id: str = ""
        self._privacy_mode = True
        self._status_message = ""
        self.hide()

    def credit_count(self) -> int:
        return len(self._credits)

    def target_credit_id(self) -> str:
        return self._target_id

    def _render_credit_lines(self) -> None:
        # each fact appears exactly once in the panel: the header carries the
        # total count, the button carries the "expires in" phrase and names
        # the target (first duration, expiry-sorted like pick_next_expiring),
        # so lines are just "title — 5d · 14d · 19d". ×N only appears when
        # missing expiries would otherwise hide the group's size.
        grouped: dict[str, list[str]] = {}
        for credit in sorted(
            self._credits,
            key=lambda candidate: str(candidate.get("expires_at") or "~"),
        ):
            title = redact_text(
                str(credit.get("title") or "Reset credit"),
                redact_emails=self._privacy_mode,
            )
            grouped.setdefault(title, []).append(format_expiry(credit))
        lines = []
        for title, notes in grouped.items():
            durations = [n.removeprefix("expires in ") for n in notes if n]
            label = title if len(durations) == len(notes) else f"{title} ×{len(notes)}"
            # NBSPs inside the duration run: if it wraps, it wraps whole —
            # never "5d · 14d ·" / orphaned "19d" across lines
            run = "\u00a0·\u00a0".join(d.replace(" ", "\u00a0") for d in durations)
            lines.append(f"{label} — {run}" if run else label)
        self.credit_lines.setText("\n".join(lines))

    def set_privacy_mode(self, enabled: bool) -> None:
        self._privacy_mode = bool(enabled)
        self._render_credit_lines()
        if self._status_message:
            self.status_line.setText(
                redact_text(
                    self._status_message,
                    redact_emails=self._privacy_mode,
                )
            )

    def set_credits(self, credits: list[dict], *, privacy_mode: bool = True) -> None:
        self._privacy_mode = bool(privacy_mode)
        candidates = [c for c in credits if isinstance(c, dict)]
        self._credits = available_reset_credits(candidates)
        self._status_message = ""
        available = self._credits
        if not available:
            self._target_id = ""
            self.count_label.setText("0 available")
            self.credit_lines.clear()
            self.redeem_button.setText("Redeem")
            self.redeem_button.setEnabled(False)
            self.status_line.hide()
            self.hide()
            return
        target = pick_next_expiring(available)
        self._target_id = str(target.get("id") or "") if target else ""
        self.count_label.setText(f"{len(available)} available")
        self._render_credit_lines()
        if target:
            note = format_expiry(target)
            self.redeem_button.setText(
                f"Redeem next ({note})" if note else "Redeem next"
            )
            self.redeem_button.setEnabled(bool(self._target_id))
        self.status_line.hide()
        self.show()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.redeem_button.setEnabled(not busy and bool(self._target_id))
        if message:
            self._status_message = message
            self.status_line.setText(
                redact_text(message, redact_emails=self._privacy_mode)
            )
            self.status_line.show()

    def show_result(self, message: str, *, ok: bool) -> None:
        self._status_message = message
        color = SOFT_GOOD if ok else SOFT_CRIT
        self.status_line.setStyleSheet(
            f"color: {color}; font-size: 11px; background: transparent;"
        )
        self.status_line.setText(redact_text(message, redact_emails=self._privacy_mode))
        self.status_line.show()

    def _emit_redeem(self) -> None:
        if self._target_id:
            self.redeem_requested.emit(self._target_id)


class OverviewView(QWidget):
    """Hybrid CodexBar overview: aggregate strip up top, provider rail on
    the left, and a per-provider stage with segmented pace meters."""

    RAIL_WIDTH = 250

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        self.aggregate = AggregateStrip()
        outer.addWidget(self.aggregate)

        split = QHBoxLayout()
        split.setSpacing(14)
        outer.addLayout(split, 1)

        rail_scroll = QScrollArea()
        rail_scroll.setWidgetResizable(True)
        rail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rail_scroll.setFixedWidth(self.RAIL_WIDTH)
        rail_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._rail_host = QWidget()
        self._rail_layout = QVBoxLayout(self._rail_host)
        self._rail_layout.setContentsMargins(0, 0, 6, 4)
        self._rail_layout.setSpacing(2)
        rail_scroll.setWidget(self._rail_host)
        split.addWidget(rail_scroll)

        stage_column = QVBoxLayout()
        stage_column.setSpacing(10)
        self.stage_scroll = QScrollArea()
        self.stage_scroll.setWidgetResizable(True)
        self.stage_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stage_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._stage_host = QWidget()
        self._stage_layout = QVBoxLayout(self._stage_host)
        self._stage_layout.setContentsMargins(4, 0, 12, 8)
        self._stage_layout.setSpacing(12)
        self.stage_scroll.setWidget(self._stage_host)
        stage_column.addWidget(self.stage_scroll, 1)
        self.reset_panel = ResetCreditPanel()
        stage_column.addWidget(self.reset_panel)
        split.addLayout(stage_column, 1)

        self.providers: list[ProviderUsage] = []
        self._rail_items: dict[str, ProviderRailItem] = {}
        self._selected = ""
        self._summary_parts: list[str] = []
        self._privacy_mode = True

    def _safe_text(self, value: object) -> str:
        return redact_text(str(value), redact_emails=self._privacy_mode)

    def summary_text(self) -> str:
        return "\n".join(self._summary_parts)

    def selected_provider(self) -> str:
        return self._selected

    def set_reset_credits(
        self, credits: list[dict], *, privacy_mode: bool = True
    ) -> None:
        self.reset_panel.set_credits(credits, privacy_mode=privacy_mode)
        self._sync_reset_panel_visibility()

    def _sync_reset_panel_visibility(self) -> None:
        """The credits are Codex rate-limit resets — only show them on Codex."""
        self.reset_panel.setVisible(
            self.reset_panel.credit_count() > 0 and self._selected == "codex"
        )

    def set_providers(
        self, providers: list[ProviderUsage], *, privacy_mode: bool = True
    ) -> None:
        self._privacy_mode = bool(privacy_mode)
        self.providers = providers
        self.reset_panel.set_privacy_mode(self._privacy_mode)
        if self._selected not in {p.provider for p in providers}:
            self._selected = providers[0].provider if providers else ""
        self.aggregate.set_data(providers, privacy_mode=self._privacy_mode)
        self._summary_parts = []
        for provider in providers:
            self._summary_parts.append(self._safe_text(provider.display_name))
            if provider.error:
                self._summary_parts.append(self._safe_text(provider.error))
        self._sync_reset_panel_visibility()
        self._rebuild_rail()
        self._rebuild_stage()

    def _select(self, provider_key: str) -> None:
        if provider_key == self._selected:
            return
        self._selected = provider_key
        for key, item in self._rail_items.items():
            item.set_selected(key == provider_key)
        self._sync_reset_panel_visibility()
        self._rebuild_stage()

    def _rebuild_rail(self) -> None:
        _clear_layout(self._rail_layout)
        self._rail_items = {}
        if not self.providers:
            self._rail_layout.addWidget(
                _label("No providers yet", size=12, color=MUTED)
            )
            self._rail_layout.addStretch(1)
            return
        for provider in sorted(
            self.providers, key=lambda p: -p.max_used_percent if not p.error else 1
        ):
            item = ProviderRailItem(provider, safe_text=self._safe_text)
            item.set_selected(provider.provider == self._selected)
            item.clicked.connect(self._select)
            self._rail_layout.addWidget(item)
            self._rail_items[provider.provider] = item
        self._rail_layout.addStretch(1)

    def _rebuild_stage(self) -> None:
        _clear_layout(self._stage_layout)
        provider = next(
            (p for p in self.providers if p.provider == self._selected), None
        )
        if provider is None:
            self._stage_layout.addWidget(
                _label(
                    "No provider data yet — refresh or enable providers in the CodexBar CLI.",
                    color=MUTED,
                )
            )
            self._stage_layout.addStretch(1)
            return

        head = QHBoxLayout()
        head.setSpacing(12)
        badge = QLabel()
        badge.setPixmap(provider_badge(provider.provider, 28))
        badge.setStyleSheet("background: transparent;")
        head.addWidget(badge)
        head.addWidget(
            _label(self._safe_text(provider.display_name), size=20, weight=800)
        )
        meta_bits = [
            bit
            for bit in (
                self._safe_text(provider.source) if provider.source else "",
                f"v{self._safe_text(provider.version)}" if provider.version else "",
            )
            if bit
        ]
        if meta_bits:
            head.addWidget(_label(" · ".join(meta_bits), size=11, color=MUTED))
        head.addStretch(1)
        if not provider.error and provider.windows:
            peak = provider.max_used_percent
            head.addWidget(
                _label(
                    f"{100 - peak:.0f}% left",
                    size=16,
                    weight=800,
                    color=color_for_percent(peak),
                )
            )
        self._stage_layout.addLayout(head)
        self._stage_layout.addWidget(hairline())

        if provider.error:
            error = _label(
                f"Error: {self._safe_text(provider.error)}", size=12, color=SOFT_CRIT
            )
            error.setWordWrap(True)
            self._stage_layout.addWidget(error)
            self._stage_layout.addStretch(1)
            return

        accent = provider_accent_color(provider.provider)
        for window in provider.windows:
            self._stage_layout.addLayout(self._window_row(window, accent))
        footer_bits = []
        if provider.credits_remaining is not None:
            footer_bits.append(f"Credits {provider.credits_remaining:g}")
        if footer_bits:
            self._stage_layout.addWidget(
                _label("   ·   ".join(footer_bits), size=11, color=MUTED)
            )
        self._stage_layout.addStretch(1)

    def _window_row(self, window: WindowUsage, accent: str) -> QVBoxLayout:
        row = QVBoxLayout()
        row.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(0)
        top.addWidget(_label(self._safe_text(window.label), size=12, weight=600))
        top.addStretch(1)
        # fixed-width columns so the dot separator and both texts line up
        # across rows regardless of "2h 2m" vs "6d 2h" string widths
        metrics = _row_metrics()
        left = 100 - window.used_percent
        left_label = _label(
            f"{left:.0f}% left",
            size=12,
            color=color_for_percent(window.used_percent),
        )
        left_label.setFixedWidth(metrics.horizontalAdvance("100% left") + 4)
        left_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        top.addWidget(left_label)
        # reset time is a clock, not a judgment — keep it neutral like the
        # rail countdowns and the Next-reset stat
        reset_bit = (
            f"resets in {window.reset_countdown}"
            if window.reset_countdown
            else self._safe_text(window.reset_description)
            if window.reset_description
            else ""
        )
        if reset_bit:
            dot = _label("·", size=12, color=MUTED)
            dot.setFixedWidth(metrics.horizontalAdvance("·") + 12)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            top.addWidget(dot)
            reset_label = _label(reset_bit, size=12, color=MUTED)
            reset_label.setFixedWidth(
                max(
                    metrics.horizontalAdvance("resets in 00d 00h"),
                    metrics.horizontalAdvance(reset_bit),
                )
                + 4
            )
            top.addWidget(reset_label)
        row.addLayout(top)
        bar = SegmentBar()
        bar.set_values(window.used_percent, window.expected_used_percent, accent)
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
        layout.setContentsMargins(4, 0, 12, 8)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(
            lambda _: self.selection_changed.emit()
        )
        controls.addWidget(self.selector)
        controls.addStretch(1)
        controls.addWidget(_caption("Peak usage per day · last 30 days"))
        layout.addLayout(controls)
        self.stats = StatStrip(
            [
                ("today", "Today peak"),
                ("avg", "30-day avg"),
                ("days", "Active days"),
            ]
        )
        layout.addWidget(self.stats)
        card, card_layout = _card()
        self.chart = BarChartWidget()
        card_layout.addWidget(self.chart)
        layout.addWidget(card, 1)
        self.message = _label("", size=12, color=MUTED)
        self.message.hide()
        layout.addWidget(self.message)

    def set_options(self, options: list[tuple[str, str, str]]) -> None:
        """options: (provider_key, window_key, label)."""
        _fill_selector(self.selector, options)

    def current_selection(self) -> tuple[str, str] | None:
        return self.selector.currentData()

    def set_series(
        self, points: list[SeriesPoint], accent: str, message: str = ""
    ) -> None:
        self.chart.set_points(points, accent)
        active = [p for p in points if p.value > 0]
        if active:
            today = points[-1].value
            avg = sum(p.value for p in active) / len(active)
            self.stats.set("today", f"{today:.0f}%", color_for_percent(today))
            self.stats.set("avg", f"{avg:.0f}%", color_for_percent(avg))
            self.stats.set("days", str(len(active)))
        else:
            for key in ("today", "avg", "days"):
                self.stats.set(key, "—", MUTED)
        self.message.setText(message)
        self.message.setVisible(bool(message))


class BurnDownView(QWidget):
    """Remaining budget vs ideal steady burn for the active window."""

    selection_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 12, 8)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(
            lambda _: self.selection_changed.emit()
        )
        controls.addWidget(self.selector)
        controls.addStretch(1)
        controls.addWidget(_caption("Remaining budget vs ideal steady burn (dashed)"))
        layout.addLayout(controls)
        self.stats = StatStrip(
            [
                ("left", "Remaining"),
                ("ideal", "Ideal now"),
                ("pace", "Vs steady burn"),
            ]
        )
        layout.addWidget(self.stats)
        card, card_layout = _card()
        self.chart = BurnDownWidget()
        card_layout.addWidget(self.chart)
        layout.addWidget(card, 1)
        self.message = _label("", size=12, color=MUTED)
        self.message.hide()
        layout.addWidget(self.message)

    def set_options(self, options: list[tuple[str, str, str]]) -> None:
        _fill_selector(self.selector, options)

    def current_selection(self) -> tuple[str, str] | None:
        return self.selector.currentData()

    def set_burn_down(
        self, burn: BurnDown | None, accent: str, message: str = ""
    ) -> None:
        self.chart.set_burn_down(burn, accent)
        if burn is not None and burn.actual:
            latest = burn.actual[-1]
            ideal = burn.ideal_remaining_at(latest.ts)
            delta = latest.value - ideal
            stance = "ahead" if delta >= 0 else "behind"
            self.stats.set(
                "left", f"{latest.value:.0f}%", color_for_percent(100 - latest.value)
            )
            self.stats.set("ideal", f"{ideal:.0f}%")
            self.stats.set(
                "pace",
                f"{abs(delta):.0f}% {stance}",
                GOOD if delta >= 0 else CRIT,
            )
        else:
            for key in ("left", "ideal", "pace"):
                self.stats.set(key, "—", MUTED)
        self.message.setText(message)
        self.message.setVisible(bool(message))


class DetailsView(QWidget):
    """Per-provider cards carrying the compact `codexbar usage` fields."""

    privacy_changed = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 12, 8)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addWidget(_caption("Everything meaningful from codexbar usage"))
        controls.addStretch(1)
        self.privacy_toggle = QCheckBox("Privacy mode")
        self.privacy_toggle.setToolTip(
            "Mask account identity in Details and the tray tooltip"
        )
        self.privacy_toggle.setChecked(True)
        self.privacy_toggle.toggled.connect(self.privacy_changed.emit)
        controls.addWidget(self.privacy_toggle)
        layout.addLayout(controls)
        layout.addWidget(hairline())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._cards_host = QWidget()
        self._cards_host.setStyleSheet("background: transparent;")
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(0, 0, 8, 0)
        self._cards_layout.setSpacing(12)
        self._cards_layout.addStretch(1)
        scroll.setWidget(self._cards_host)
        layout.addWidget(scroll, 1)
        self._plain = ""

    def set_privacy_mode(self, enabled: bool) -> None:
        self.privacy_toggle.blockSignals(True)
        self.privacy_toggle.setChecked(enabled)
        self.privacy_toggle.blockSignals(False)

    def set_text(self, value: str) -> None:
        self._plain = value
        # Rebuild cards: one per provider block (blocks separated by blank
        # lines); keep the trailing stretch.
        _clear_layout(self._cards_layout, keep_tail=1)
        blocks = [b for b in value.split("\n\n") if b.strip()]
        for index, block in enumerate(blocks):
            head, *rest = block.splitlines()
            card, card_layout = _card()
            card_layout.addWidget(_mono(head, weight=700))
            if rest:
                card_layout.addWidget(hairline())
                card_layout.addWidget(_mono("\n".join(rest), MUTED))
            self._cards_layout.insertWidget(index, card)

    def plain_text(self) -> str:
        return self._plain


def window_options(providers: list[ProviderUsage]) -> list[tuple[str, str, str]]:
    """Selector entries for history/burn-down: one per provider window."""
    options: list[tuple[str, str, str]] = []
    for provider in providers:
        if provider.error:
            continue
        for window in provider.windows:
            options.append(
                (
                    provider.provider,
                    window.key,
                    f"{provider.display_name} — {window.label}",
                )
            )
    return options


def latest_reset_at(
    providers: list[ProviderUsage], provider_key: str, window_key: str
) -> tuple[dt.datetime | None, int | None]:
    for provider in providers:
        if provider.provider != provider_key:
            continue
        for window in provider.windows:
            if window.key == window_key:
                return parse_iso_datetime(window.resets_at), window.window_minutes
    return None, None
