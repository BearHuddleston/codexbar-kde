# Third-Party Notices — CodexBar KDE AppImage

This notice covers the pinned x86_64 AppImage assembled by
`scripts/build_appimage.sh`. The project source remains MIT-licensed, but the
binary AppImage includes the GPL build of PyQt6. Distribution of the combined
AppImage must therefore satisfy **GPL version 3**. No Riverbank commercial
PyQt6 license is represented or implied by this build.

The package-level SPDX 2.3 inventory is
`CodexBar_KDE-x86_64.spdx.json`. Complete license texts are installed in
`usr/share/licenses/codexbar-kde/` inside the AppImage.

## Bundled components

### CodexBar KDE 0.1.0 — MIT

- Project source: <https://github.com/BearHuddleston/codexbar-kde>
- License text: `LICENSE`

The MIT license is GPL-compatible and permits the project source to be
redistributed under the GPL-compatible terms required by this combined binary.
The upstream project source itself remains available under MIT.

### PyQt6 6.7.1 — GPL v3

The pinned PyPI wheel identifies its license as `GPL v3`. PyQt6 is not LGPL.
The public wheel used here is not a commercial-license grant.

- Binary wheel: <https://files.pythonhosted.org/packages/e4/d3/8789879c05cfe06127c4b59258632bd175fcdd9eaaadaf0c897b458fb91d/PyQt6-6.7.1-1-cp38-abi3-manylinux_2_28_x86_64.whl>
- Binary SHA-256: `c2f202b7941aa74e5c7e1463a6f27d9131dbc1e6cabe85571d7364f5b3de7397`
- Corresponding source: <https://files.pythonhosted.org/packages/d1/f9/b0c2ba758b14a7219e076138ea1e738c068bf388e64eee68f3df4fc96f5a/PyQt6-6.7.1.tar.gz>
- Source SHA-256: `3672a82ccd3a62e99ab200a13903421e2928e399fda25ced98d140313ad59cb9`
- License reference: <https://www.riverbankcomputing.com/static/Docs/PyQt6/introduction.html#license>

The full GPLv3 text is included in `GPL-3.0-and-LGPL-3.0.txt` inside the
AppImage.

### Qt 6.7.1 — LGPL v3 and GPL v3 components

The PyQt6-Qt6 wheel metadata says LGPL v3, while its own license notice states
that most Qt functionality is LGPLv3 and some tools/add-ons are GPLv3. The
wheel contains more modules than CodexBar KDE imports, so this notice
conservatively records both obligations. CodexBar KDE imports only Qt Core,
GUI, and Widgets through PyQt6.

- Binary wheel: <https://files.pythonhosted.org/packages/57/56/b653a011af4b821b1ad0b20b554f351cf8331127ffd4cd60696bc8576655/PyQt6_Qt6-6.7.1-py3-none-manylinux_2_28_x86_64.whl>
- Binary SHA-256: `9fbab2a96d72d77d16021e259ef86a1a3c87adb0e7eebcc92df0d39f3fdf7e27`
- Corresponding source: <https://download.qt.io/archive/qt/6.7/6.7.1/single/qt-everywhere-src-6.7.1.tar.xz>
- Source SHA-256: `38dbf2768776e875ed5cdea8cccf1a240512a29769768084430914c4a33bedc4`
- License reference: <https://doc.qt.io/qt-6.7/licensing.html>

The wheel's complete GPLv3/LGPLv3 notice is copied into
`GPL-3.0-and-LGPL-3.0.txt`. Qt libraries remain dynamically linked. Recipients
can extract the AppImage with `--appimage-extract`, replace compatible shared
libraries, and repack it using the published build script.

### PyQt6-sip 13.8.0 — permissive SIP license

- Binary wheel: <https://files.pythonhosted.org/packages/4f/db/f453a866d5bdadc98a48f457f6af0794ea0de5b806156eb9d74c7b25a08e/PyQt6_sip-13.8.0-cp311-cp311-manylinux_2_5_x86_64.manylinux1_x86_64.whl>
- Binary SHA-256: `a5c086b7c9c7996ea9b7522646cc24eebbf3591ec9dd38f65c0a3fdb0dbeaac7`
- Corresponding source: <https://files.pythonhosted.org/packages/e9/b7/95ac49b181096ef40144ef05aff8de7c9657de7916a70533d202ed9f0fd2/PyQt6_sip-13.8.0.tar.gz>
- Source SHA-256: `2f74cf3d6d9cab5152bd9f49d570b2dfb87553ebb5c4919abfde27f5b9fd69d4`

Its complete license is copied into `PyQt6-sip.txt` inside the AppImage.

### Python 3.11.14 AppImage base — aggregate; CPython is PSF-2.0

The pinned base is an upstream `python-appimage` aggregate containing CPython
and supporting runtime components. Their embedded license files are retained.
CPython's license is centralized as `Python-3.11.14.txt`.

- Binary base: <https://github.com/niess/python-appimage/releases/download/python3.11/python3.11.14-cp311-cp311-manylinux_2_28_x86_64.AppImage>
- Binary SHA-256: `89ff05124b2fcbecbd46006be5b477760907cccb13518054fa67a4356b8215f3`
- Build project tag: <https://github.com/niess/python-appimage/tree/python3.11>
- CPython corresponding source: <https://www.python.org/ftp/python/3.11.14/Python-3.11.14.tar.xz>
- CPython source SHA-256: `8d3ed8ec5c88c1c95f5e558612a725450d2452813ddad5e58fdb1a53b1209b78`

## Source availability and release condition

When distributing the AppImage from a network location, publish this notice,
the SPDX file, this repository's exact source revision and build scripts, and
the listed corresponding-source archives or equivalent no-charge access next
to the binary. Upstream URLs alone are not a substitute for ensuring source
remains available for the period required by the applicable licenses.

This inventory is an engineering compliance record, not legal advice. See
`docs/appimage-licensing.md` for the release checklist and unresolved legal
sign-off requirement.
