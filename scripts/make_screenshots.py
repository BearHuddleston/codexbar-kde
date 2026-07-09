"""Render README screenshots offscreen using synthetic demo data.

Usage:
    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py

Writes one PNG per dashboard view into docs/screenshots/. No real
`codexbar usage` output is used, so no account identity can leak into
the repository.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from codexbar_kde.app import DashboardWindow  # noqa: E402
from codexbar_kde.history import HistoryStore  # noqa: E402
from codexbar_kde.model import normalize_payload  # noqa: E402

OUT_DIR = REPO_ROOT / "docs" / "screenshots"

NOW = dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def demo_payload() -> list[dict]:
    """A realistic-looking, fully synthetic codexbar usage payload."""
    return [
        {
            "provider": "codex",
            "source": "oauth",
            "version": "0.9.4",
            "credits": {"remaining": 0, "updatedAt": iso(NOW)},
            "pace": {
                "primary": {
                    "deltaPercent": -17,
                    "expectedUsedPercent": 55,
                    "willLastToReset": True,
                },
                "secondary": {
                    "deltaPercent": -24,
                    "expectedUsedPercent": 66,
                    "willLastToReset": True,
                },
            },
            "usage": {
                "updatedAt": iso(NOW),
                "primary": {
                    "label": "5h/session",
                    "usedPercent": 38,
                    "windowMinutes": 300,
                    "resetsAt": iso(NOW + dt.timedelta(hours=2)),
                },
                "secondary": {
                    "label": "weekly",
                    "usedPercent": 42,
                    "windowMinutes": 10080,
                    "resetsAt": iso(NOW + dt.timedelta(days=3, hours=6)),
                },
                "codexResetCredits": {
                    "availableCount": 3,
                    "credits": [
                        {
                            "id": f"RateLimitResetCredit_demo{i}",
                            "title": "Full reset (Weekly + 5 hr)",
                            "description": "Demo reset credit.",
                            "status": "available",
                            "reset_type": "codex_rate_limits",
                            "granted_at": iso(NOW - dt.timedelta(days=20 - i * 5)),
                            "expires_at": iso(NOW + dt.timedelta(days=8 + i * 6)),
                        }
                        for i in range(3)
                    ],
                },
            },
        },
        {
            "provider": "claude",
            "source": "oauth",
            "version": "1.2.0",
            "pace": {
                "primary": {
                    "deltaPercent": 6,
                    "expectedUsedPercent": 60,
                    "willLastToReset": True,
                },
            },
            "usage": {
                "updatedAt": iso(NOW),
                "primary": {
                    "label": "5h/session",
                    "usedPercent": 66,
                    "windowMinutes": 300,
                    "resetsAt": iso(NOW + dt.timedelta(hours=1, minutes=40)),
                },
                "secondary": {
                    "label": "weekly",
                    "usedPercent": 31,
                    "windowMinutes": 10080,
                    "resetsAt": iso(NOW + dt.timedelta(days=5, hours=2)),
                },
            },
        },
        {
            "provider": "gemini",
            "source": "oauth",
            "version": "0.4.1",
            "usage": {
                "updatedAt": iso(NOW),
                "primary": {
                    "label": "daily",
                    "usedPercent": 12,
                    "windowMinutes": 1440,
                    "resetsAt": iso(NOW + dt.timedelta(hours=9)),
                },
            },
        },
        {
            "provider": "copilot",
            "source": "oauth",
            "version": "2.1.0",
            "usage": {
                "updatedAt": iso(NOW),
                "primary": {
                    "label": "monthly premium",
                    "usedPercent": 91,
                    "windowMinutes": 43200,
                    "resetsAt": iso(NOW + dt.timedelta(days=11)),
                },
            },
        },
    ]


def seed_history(store: HistoryStore) -> None:
    """Write ~30 days of plausible samples so History/Burn-down have data."""
    import math
    import random

    random.seed(7)
    lines: list[str] = []

    def add(ts: dt.datetime, provider: str, windows: dict[str, float]) -> None:
        lines.append(
            json.dumps({"ts": ts.isoformat(), "provider": provider, "windows": windows})
        )

    # Daily peaks for the last 30 days (a couple of samples per day).
    for offset in range(30, 0, -1):
        day = NOW - dt.timedelta(days=offset)
        wave = 0.5 + 0.5 * math.sin(offset / 4.5)
        for hour in (10, 15, 20):
            ts = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            add(
                ts,
                "codex",
                {
                    "primary": max(
                        0.0, min(96.0, 25 + 60 * wave + random.uniform(-8, 8))
                    ),
                    "secondary": max(
                        0.0, min(96.0, 20 + 45 * wave + random.uniform(-5, 5))
                    ),
                },
            )
            add(
                ts,
                "claude",
                {
                    "primary": max(
                        0.0, min(96.0, 35 + 50 * (1 - wave) + random.uniform(-8, 8))
                    ),
                    "secondary": max(
                        0.0, min(96.0, 15 + 30 * (1 - wave) + random.uniform(-5, 5))
                    ),
                },
            )
            add(
                ts,
                "gemini",
                {"primary": max(0.0, 5 + 18 * wave + random.uniform(-3, 3))},
            )
            add(
                ts,
                "copilot",
                {"primary": min(96.0, 60 + offset * -0.5 + 30 + random.uniform(-2, 2))},
            )

    # Intra-window samples for the codex 5h burn-down (window started 3h ago).
    window_start = NOW + dt.timedelta(hours=2) - dt.timedelta(minutes=300)
    minutes_elapsed = int((NOW - window_start).total_seconds() // 60)
    for m in range(0, minutes_elapsed + 1, 10):
        ts = window_start + dt.timedelta(minutes=m)
        used = 38 * (m / max(1, minutes_elapsed)) ** 1.25
        add(ts, "codex", {"primary": round(used, 1), "secondary": 42.0})

    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(["codexbar-kde-screenshots"])

    with tempfile.TemporaryDirectory(prefix="codexbar-kde-shots-") as tmp:
        store = HistoryStore(Path(tmp) / "history.jsonl")
        seed_history(store)

        payload = demo_payload()
        providers = normalize_payload(payload)
        window = DashboardWindow(refresh_seconds=3600, history_store=store)
        window.resize(1180, 720)
        window.set_providers(providers, raw_payload=payload)
        window.show()
        app.processEvents()

        slugs = {
            "Overview": "overview",
            "History": "history",
            "Burn-down": "burndown",
            "Details": "details",
        }
        for name in window.view_names():
            window.show_view(name)
            app.processEvents()
            pixmap = window.grab()
            out = OUT_DIR / f"{slugs.get(name, name.lower())}.png"
            pixmap.save(str(out), "PNG")
            print(
                f"wrote {out.relative_to(REPO_ROOT)} ({pixmap.width()}x{pixmap.height()})"
            )
        window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
