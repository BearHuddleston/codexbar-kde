"""Shared test fixtures: sandbox QSettings so no test writes ~/.config."""

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings  # noqa: E402

_settings_tmp = tempfile.TemporaryDirectory()
for _fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
    QSettings.setPath(_fmt, QSettings.Scope.UserScope, _settings_tmp.name)
