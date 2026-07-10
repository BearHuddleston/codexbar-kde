# AppImage Licensing Review

Date: 2026-07-10
Scope: the CodexBar KDE 0.2.0 pinned x86_64 AppImage built by `scripts/build_appimage.sh`

This is an engineering review, not legal advice. Public distribution still
requires approval from whoever is responsible for the project's license
compliance.

## Findings

1. **The PyPI PyQt6 wheel is GPLv3.** Its metadata says `GPL v3`; it is not a
   commercial wheel or a commercial-license grant. Riverbank documents PyQt6
   as GPLv3/commercial dual-licensed and not LGPL.
2. **The combined AppImage must be distributed compatibly with GPLv3.** The
   project's MIT source license is GPL-compatible and remains valid for the
   project source, but it does not remove the binary distribution's GPLv3
   obligations.
3. **The Qt wheel is an aggregate.** Its metadata says LGPLv3, while its
   included notice says most functionality is LGPLv3 and some tools/add-ons
   are GPLv3. The wheel contains modules CodexBar KDE does not import, so the
   notice and SBOM conservatively record both.
4. **The Python base is also an aggregate.** CPython is PSF-2.0, and the base
   contains additional runtime components with their own embedded notices.
   Those files are retained; CPython's license is additionally centralized.
5. **Corresponding-source access must be maintained.** Exact upstream source
   URLs and SHA-256 values are recorded in `THIRD_PARTY_NOTICES.md`. A release
   publisher remains responsible for keeping equivalent no-charge access
   available for the period required by the licenses; merely assuming an
   upstream URL will remain live is insufficient.

## Artifacts produced by the build

The build installs these files in the AppImage:

- `usr/share/doc/codexbar-kde/THIRD_PARTY_NOTICES.md`
- `usr/share/doc/codexbar-kde/CodexBar_KDE-x86_64.spdx.json`
- `usr/share/licenses/codexbar-kde/CodexBar-KDE-MIT.txt`
- `usr/share/licenses/codexbar-kde/GPL-3.0-and-LGPL-3.0.txt`
- `usr/share/licenses/codexbar-kde/PyQt6-sip.txt`
- `usr/share/licenses/codexbar-kde/Python-3.11.14.txt`

The notice and SPDX package SBOM are also emitted beside the AppImage and
uploaded by CI.

## LGPL replacement path

Qt remains dynamically linked. A recipient can:

1. extract the image with
   `CodexBar_KDE-x86_64.AppImage --appimage-extract`;
2. replace compatible Qt shared libraries below
   `squashfs-root/opt/python3.11/lib/python3.11/site-packages/PyQt6/Qt6/lib/`;
3. rebuild using the published `scripts/build_appimage.sh` inputs or a
   compatible AppImage tool.

The AppImage does not use signature enforcement or other measures to prevent a
recipient from running a modified copy.

## Release checklist

Do not publish an AppImage release until all items are complete:

- [ ] Legal/compliance owner approves GPLv3 distribution of the combined app.
- [ ] The release commit and complete build scripts are publicly accessible.
- [ ] AppImage, `THIRD_PARTY_NOTICES.md`, and the SPDX JSON are uploaded
      together.
- [ ] Exact PyQt6, Qt, PyQt6-sip, CPython, and Python-base source archives are
      available at no charge from the release location or a maintained source
      mirror, with hashes matching the notice.
- [ ] The assembled AppImage contains all six notice/license artifacts listed
      above and retains upstream `.dist-info`/runtime license files.
- [ ] The release artifact passes the cross-umask reproducibility comparison,
      ABI audit, and runtime smoke tests.
- [ ] Any change to pinned wheels, the Python base, or bundled Qt modules is
      followed by a new license inventory and SBOM review.
- [ ] Third-party code embedded within Qt and the Python base is reviewed for
      the release jurisdiction and distribution method.

## Authoritative references

- PyQt6 licensing:
  <https://www.riverbankcomputing.com/static/Docs/PyQt6/introduction.html#license>
- Riverbank licensing FAQ:
  <https://www.riverbankcomputing.com/commercial/license-faq>
- Qt 6.7 licensing: <https://doc.qt.io/qt-6.7/licensing.html>
- GPLv3 text: <https://www.gnu.org/licenses/gpl-3.0.html>
- LGPLv3 text: <https://www.gnu.org/licenses/lgpl-3.0.html>
- SPDX 2.3 package information:
  <https://spdx.github.io/spdx-spec/v2.3/package-information/>
