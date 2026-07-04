# CodexBar KDE

Local PyQt6 dashboard for AI subscription usage on KDE/Linux, built on top of the [CodexBar](https://codexbar.app) CLI (`codexbar usage --format json`). This is an independent companion app — not the official CodexBar macOS app — in the same spirit as other unofficial Linux integrations (codexbar-waybar, KodexBar, Codexbar GNOME).

Privacy model:

- Calls only `/usr/bin/codexbar usage --format json --json-only --pretty` by default.
- The Codex reset-credit redeem button additionally reads the OAuth token from `~/.codex/auth.json` (written by `codex login`) and POSTs to `https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume` — the same endpoint the official Codex desktop/VS Code extension uses. It only spends credits OpenAI granted to the account; a confirmation dialog is required and nothing is redeemed automatically.
- Does not read provider token/cookie files directly.
- Does not store credentials.
- Local cost/log scanning is not used.
- Account identity can appear in the tray tooltip because the user explicitly asked to display meaningful fields from their own `codexbar usage` output.

Run:

```sh
codexbar-kde
```

Privacy-safe terminal summary:

```sh
codexbar-kde --once
```

GUI smoke test:

```sh
QT_QPA_PLATFORM=offscreen codexbar-kde --test-render
```

UI notes:

- CodexBar-inspired flat dark shell (codexbar.app design language): sidebar navigation, hairline separators, thin 6px meters — no heavy cards.
- Four statistic views, also reachable from the tray "Views" submenu:
  - **Overview** — per-provider sections in CodexBar menu style: `N% left`, reset countdowns, thin provider-accent meters, pace notes, credits. A **Codex reset credits** panel at the top lists banked credits sorted by expiry and offers a one-click **Redeem next (expires in Xd)** button for the credit closest to expiring (confirmation dialog, then automatic usage refresh).
  - **History** — daily peak-usage bar chart (last 30 days) per provider window, teal/accent bars like the CodexBar History widget.
  - **Burn-down** — remaining budget vs a dashed ideal steady-burn line for the active window, like the CodexBar Burn Down widget.
  - **Details** — monospace compact dump of every meaningful `codexbar usage` field.
- Usage history is sampled on every refresh into `~/.local/state/codexbar-kde/history.jsonl` (JSONL, pruned to 60 days, corrupt lines skipped). Charts fill in as history accrues.
- Provider errors stay inline in Overview without hiding healthy providers.

Tray hover behavior:

- KDE/QSystemTrayIcon tooltips are still plain text, not full custom HTML/CSS popovers.
- The tooltip uses Nerd Font / Font Awesome glyphs when available locally: dashboard, check, warning, user, shield, clock/calendar, bolt, credit-card, and refresh icons.
- This machine has Nerd Font glyph fallback via `FantasqueSansM Nerd Font`, verified with fontconfig for the glyph codepoints used.
- The tooltip also uses provider count, divider line, peak usage, unicode usage bars, reset descriptions, pace, credits, and provider errors.
- Low-value raw JSON is omitted: empty arrays, duplicate provider IDs, reset-credit IDs, boilerplate descriptions, grant dates, reset types, null tertiary windows, and raw window-minute values.
- Identical reset credits are grouped into one line (`Full reset (Weekly + 5 hr) ×4 · next expires …`).
- `Updated:` shows a relative age (`just now`, `25m ago`) instead of a raw ISO timestamp.
- Click the tray icon to open the full dashboard.
- Right-click the tray icon for Open / Refresh / Quit.
