# AGENTS.md — Omameter (`paulimfavarato.omameter`)

Technical guide to the context, invariants and workflow that agents need to keep developing this plugin safely across sessions.

---

## 1. Overview & Architecture

Native **Omarchy / Quickshell** plugin (`"kinds": ["service", "bar-widget"]`, `"keepLoaded": true`) that monitors AI subscription limits on Linux/Hyprland.

```
Omarchy Top Bar (1+ monitors)
  └── OmameterWidget.qml (BarWidget + KeyboardPanel 396px)
        ├── Model.js               (pure JS helpers: formatting, sanitization, palette; describeLine = sole owner of a metric line's text; accountHeader/displayAccountName = the single naming + privacy rule; tr(strings, key, fb) = the single i18n fallback, views only delegate to `t()`; metricPickerLabel uses the daemon's `titleNamesWindow`)
        ├── Settings.js            (copy of the manifest's settings schema; defaultSettings/normalize/applyPatch/normalizePatch/stepThreshold with linked thresholds identical to Python; reconcile/markWritten = pure shell.json → daemon rule)
        ├── Navigation.js          (pure state machine for the popup and accounts: reduce(state, action, ctx) → {state, effects}; viewMode/dashboardTab/subPage, drafts, canSubmit per form, the auth_flow rule, single cancelAuth, resetScroll, clearing of secret drafts; forms that validate live (API key, hub, hub key, manual code) wait for the command: awaiting {form,id} via commandSent/commandSettled, close only on success, on failure stay open with drafts + formError)
        ├── Protocol.js            (pure reducer for the NDJSON connection: encode → {state,line,id} (id + pending), ingest(line) → {state,events}, settle, daemonDown (fails pending commands), outcome(id) → pending|ok|failed)
        └── ui/*.qml               (PriorityTab, AllUsageTab, MetricBar, PriorityRing,
                                    AccountsView, SettingsView, BrandMark, PlainText;
                                    PriorityRow (Priority tab row: describeLine + signals
                                    openAllUsage/toggleDisplayMode/toggleResetMode/togglePin,
                                    no `service`; PriorityTab wires the signals; fixed pin slot,
                                    hover only via opacity/enabled);
                                    Settings screen blocks: SettingToggleRow, SettingChoiceRow,
                                    ThresholdStepper (schema limits via Settings.js), AlertMatrixRow;
                                    shared stateless blocks, theme passed through:
                                    Pill (badge with padding/radius as parameters), EmptyState ("no accounts" card),
                                    ConfirmStep (2-step destructive action; `confirming` comes from the owner, emits signals),
                                    ListCard (clickable account/hub row), FormField (caption + TextField
                                    + inline button + error; `fieldFocused` feeds `inputFocused`))
              │ (reactive properties + RPC calls)
              ▼
      OmameterService.qml         (service singleton in the shell + global ShellIpc; changeSettings = the single settings write path: daemon + shell.json; Protocol.js glue: every command function returns its id, signal commandSettled(id, ok, error), commandOutcome(id); detectingLocal = last detect_local_credentials still pending; own Commons.Translations for its messages, sends `set_language` when the daemon becomes ready and whenever Omarchy's language changes)
              │ stdin/stdout NDJSON (single Process shared across monitors)
              ▼
      backend/daemon.py            (Python 3 stdlib daemon, zero pip dependencies)
        ├── backend/refresh.py     (AccountRefresher: gate → fetch with offline retry → record; context_for/claim share the secondary cache; injectable Clock)
        ├── backend/accounts.py    (AccountLifecycle: admit with an explicit AdmissionRule per path (OAUTH_LOGIN/LOCAL_IMPORT/API_KEY), remove clears everything keyed by the account id (store, vault, error+backoff, secondary cache, pace), remove_hub, sync_hub)
        ├── backend/backoff.py     (RefreshBackoff 5→60 min, OFFLINE_RETRY_DELAYS, REFRESH_GRACE_SEC, SECONDARY_READ_INTERVAL_SEC)
        ├── backend/secondary.py   (SecondaryReads/SecondaryCache: in-memory per-account secondary reads; is_due/read keep|clear/invalidate; keys are constants of the owning module)
        ├── backend/models.py      (Account, ProviderSnapshot, MetricLine, PROVIDER_CATALOG, ISO-8601 helpers; title_names_window decides from the untranslated title whether it already names its window)
        ├── backend/providers/*    (13 providers + base + registry; each one owns its URLs, headers, quirks and mapper)
        ├── backend/providers/base.py    (ProviderError, FetchContext, raise_for_http_error, send_mapped, read_json, is_auth_failure, TERMINAL_REFRESH_ERRORS, oauth_error_code)
        ├── backend/providers/session.py (OAuth engine: OAuthRefresh per provider; renew/fresh_credential/authorized/send_with_retry/with_fresh_token; renews early, keeps a non-expired token if renewal fails, 1 retry on 401, never renews a hub account)
        ├── backend/jwt_claims.py  (decode_jwt_payload + jwt_expiry; the single JWT decoder, used by auth.py and providers)
        ├── backend/auth.py        (OAuth PKCE dual-stack loopback, Device Code, API Key)
        ├── backend/local_detect.py(opt-in local scan for 1-click import)
        ├── backend/cliproxy.py    (CLIProxyAPI Hubs sync & proxy fetch)
        ├── backend/pace.py        (Pace Marks, Burning Fast with 90% hysteresis, notifications)
        ├── backend/priority.py    (PriorityBoard, Unify Accounts, Top Bar strip, hiddenMetrics)
        ├── backend/resets.py      (Rate-Limit Resets: Claude, Codex, Z.ai)
        ├── backend/http_client.py (HTTP/HTTPS + SOCKS5/HTTP proxy + redaction + default UA)
        ├── backend/i18n.py        (daemon-side translations from the same i18n/<lang>.json catalogs as the QML; tr(key, fallback, *args) with %1..%9; lookup chain language → base language → en → fallback; set_language; metric_title/unit_label/data_text translate English data stored in snapshots (metric titles, value labels/subtitles, provider notices, reset unavailable reasons) at the dashboard edge via metric.*/unit.*/data.* keys, identity in English)
        ├── backend/settings.py    (reads manifest.json barWidget.schema: DEFAULT_SETTINGS, normalize_settings, apply_patch, PriorityLimits with schema limits; unreadable manifest → SettingsSchemaError)
        ├── backend/store.py       (JSON persistence + layout undo stack; settings via backend/settings.py)
        ├── backend/vault.py       (secret-tool / libsecret with 0600 fallback in vault.json)
        ├── backend/limits_envelope.py (schema omameter.limits.v2)
        └── backend/analytics/     (Phase 1 quota history + Phase 2 opt-in local token & API-equiv cost analytics:
                                    db.py (SQLite WAL ledger: quota_samples, token_events, file_scan_state),
                                    parsers.py (incremental byte-offset + 64-byte guard-hash scanners for Claude Code,
                                    GPT Codex, Antigravity, OpenCode — strictly local harnesses; ~/.t3 only via the
                                    `includeT3Antigravity` opt-in, limited to T3 Code's Antigravity conversations),
                                    pricing.py + pricing_catalog.json (50 bundled models, context/speed tiers,
                                    on-demand LiteLLM sync via HTTPClient max_bytes=16 MiB, overrides & aliases),
                                    service.py (quota recording, streaming progress scans, aggregation, Since Reset),
                                    cli.py (NDJSON worker `--serve` spawned only while the Analytics window is open))

analytics/                        (standalone Quickshell floating window; 0 MB in omarchy-shell when closed:
                                   shell.qml, BarTrendChart.qml, QuotaLineChart.qml, PlainText.qml, qmldir;
                                   reads ~/.local/state/omarchy/current/theme/{colors,shell}.toml + ~/.config/omarchy/shell.toml
                                   and Hyprland decoration:rounding live)
bin/omameter                      (read-only CLI over the local cache; exit codes 0 / 2 / 4)
bin/omameter-analytics            (launches or focuses the standalone Analytics window floating & centered via
                                   Hyprland Lua `hl.window_rule` / `hl.dsp.focus` with legacy fallbacks)

i18n/en.json, i18n/pt-BR.json     (UI catalogs shared by QML/JS and backend/i18n.py)
scripts/check_i18n.py             (JSON validity of manifest, catalogs and fixtures; key/placeholder/plural parity across
                                   catalogs; every tr("key","fb") (Python) and t/tn/tr/_trArg/_nameList / `key:…, fb:…`
                                   (QML/JS) key exists with English text equal to the fallback; no unused catalog keys
                                   (dynamic prefixes card., metric., unit., data.); run by tests/test_i18n.py)
install.sh                        (copies the plugin via tar, without symlinks; excludes tests/, scripts/, .github/, .gitignore)
uninstall.sh                      (disables the plugin, removes it and the CLI wrappers; --purge also deletes ~/.config/omameter,
                                   the cache, ~/.local/share/omameter and the secret-tool entries)
.github/workflows/ci.yml          (CI: Python unittest, node --test, scripts/check_i18n.py)
```

Translation boundary: snapshots, the cache and the `bin/omameter` CLI stay English (language-neutral); the daemon translates only when building the dashboard / user-facing text.

### On-disk paths on the user's system
- **Working repository:** the repository root
- **Installed plugin (copy without symlinks):** `~/.config/omarchy/plugins/paulimfavarato.omameter`
- **Installed CLIs:** `~/.local/bin/omameter`, `~/.local/bin/omameter-analytics`
- **Real user data (NEVER delete/corrupt in tests):**
  - `~/.config/omameter/` (`accounts.json`, `settings.json`, `layout.json`, `analytics.json`, `proxy.json`)
  - `$XDG_CACHE_HOME/omameter/` (`snapshots.json`, `litellm_rates.json`; default `~/.cache/omameter/`, see `CACHE_DIR` in `backend/store.py`)
  - `~/.local/share/omameter/` (`analytics.db`, `vault.json` `0600` fallback when `secret-tool` is not available)

---

## 2. Inviolable Rules (Gotchas already solved)

### QML / Quickshell / Omarchy
1. **Never use a raw `Text {`** — always use [`ui/PlainText.qml`](ui/PlainText.qml) (forces `textFormat: Text.PlainText` and prevents Qt rich-text injection) and pass dynamic external strings through `Model.cleanText(...)` / `Model.autoTextSafe(...)`.
2. **Anchors along the positioner's axis are forbidden on direct children of `Row`/`Column`** — on a `Row` child, never `anchors.left`, `right`, `horizontalCenter`, `fill` or `centerIn`; on a `Column` child, never `anchors.top`, `bottom`, `verticalCenter`, `fill` or `centerIn`. They spam `WARN scene: QML Row/Column: ... will not function` and break clicks/hover. Cross-axis anchors are legal and the code uses them (`verticalCenter`/`top`/`bottom` on a `Row` child; `horizontalCenter`/`left`/`right` on a `Column` child). For a clickable area covering a `Row`/`Column`, wrap it in an `Item` and put the `MouseArea` as a **sibling**.
3. **Zero layout jumps on hover** — in [`ui/MetricBar.qml`](ui/MetricBar.qml) and [`ui/PriorityTab.qml`](ui/PriorityTab.qml), keep width and height constant between normal and hover states; cross-fade with `opacity` + `enabled` instead of toggling `visible` on action buttons.
4. **Animations always via `Style.duration(...)`** — never hardcode `duration: 150` (respects Omarchy's *reduced motion*).
5. **No symlinks inside the plugin directory** — `omarchy plugin validate` rejects symlinks. [`install.sh`](install.sh) copies via `tar` without symlinks.
6. **Upgrades require `omarchy-restart-shell`** — `omarchy-shell shell rescanPlugins` does **not** reload QML already compiled in memory nor restart a running `backend/daemon.py`. [`install.sh`](install.sh) already detects a reinstall and calls `omarchy-restart-shell` automatically. Never use `omarchy-refresh-shell` (it resets the user's `shell.json`).
7. **Settings: one source and one write path** — a new settings key goes first into the `schema` (and `defaults`) of [`manifest.json`](manifest.json); then into [`Settings.js`](Settings.js) (the parity tests `tests/test_settings.mjs` / `tests/test_settings.py` enforce the rest; shared edge cases live in `tests/fixtures/settings_cases.json`). To change a setting from QML call only `service.changeSettings(patch)` (normalizes, sends `update_settings` and writes `shell.json`, marking the echo as seen); never `sendCmd({cmd:"update_settings"})` nor `setBarWidget` by hand. External edits to `shell.json` go through `syncWidgetSettings` → `Settings.reconcile` (the first read only records; with the daemon down it stays pending until `ready`). In the backend read normalized settings (`normalize_settings`/`PriorityLimits.from_settings`), never `settings.get(k, literal)`.
8. **i18n always in sync, English-only repo** — every user-visible string in QML/JS/Python goes through the catalogs (QML `t()`/`tn()`; Python `backend.i18n.tr()`), and is added to **both** [`i18n/en.json`](i18n/en.json) and [`i18n/pt-BR.json`](i18n/pt-BR.json) with exact key parity; the English catalog value must equal the fallback written in code. `python3 scripts/check_i18n.py` must pass. The whole repo (code, comments, docs, tests) is English-only; only `i18n/pt-BR.json` holds Portuguese.
9. **Standalone [`analytics/`](analytics) window is self-contained & matches Omarchy's active theme** — Quickshell blocks parent-directory imports (`qrc:/qs-blackhole`), so never `import "../..."` inside `analytics/`; use [`analytics/PlainText.qml`](analytics/PlainText.qml). Read `~/.local/state/omarchy/current/theme/{colors,shell}.toml` (+ `~/.config/omarchy/shell.toml`), use `"monospace"` (`Style.font.family`), and match Hyprland's `decoration:rounding` (`cornerRadius`). [`bin/omameter-analytics`](bin/omameter-analytics) launches floating & centered via `hyprctl eval 'hl.window_rule(...)'` and focuses via `hl.dsp.focus(...)` (with legacy `windowrulev2`/`focuswindow` fallbacks).

### Backend / OAuth / Security
1. **Default `User-Agent` in [`backend/http_client.py`](backend/http_client.py)** — keep `User-Agent: Omameter/1.0` when the caller does not send a `User-Agent`. Without it, `Python-urllib/3.x` gets **HTTP 403 (Cloudflare Error 1010)** on `https://platform.claude.com/v1/oauth/token`.
2. **Dual-stack OAuth loopback in [`backend/auth.py`](backend/auth.py)** — `LoopbackCallbackServer` must keep listening on both `127.0.0.1` and `::1` when `redirect_host == "localhost"`, because Linux resolves `localhost` to `::1` first.
3. **Preserve `auth_flow` when the popup reopens** — when the browser steals focus, the Omarchy popup closes. The rule lives only in [`Navigation.js`](Navigation.js) (actions `popupOpened` and `authSessionChanged`): with `authSession.active || authSession.status === "error"`, reopening shows `viewMode = "accounts"` + `subPage = "auth_flow"`. All popup and [`ui/AccountsView.qml`](ui/AccountsView.qml) navigation goes through `OmameterWidget.dispatch(action)` → `Navigation.reduce`; do not write `viewMode`/`subPage`/drafts nor call `resetScroll`/`cancelAuth` by hand in views, and use `Navigation.canSubmit(nav, form)` for buttons' `enabled`.
4. **Zero secrets in NDJSON IPC or logs** — `detect_local_credentials` and `emit_state` must never send tokens, refresh tokens or API keys to the QML; use `redact_secrets(...)` on error messages.
5. **Local discovery is strictly opt-in** — [`backend/local_detect.py`](backend/local_detect.py) only reads local files/CLIs when the user clicks *"Detect Local Logins"*, and imports a copy into our `CredentialVault` (never continuous background scraping).
6. **Zero idle 30s re-emission** — [`backend/daemon.py`](backend/daemon.py) only re-emits `state` in the idle loop when `nextRefreshAt` arrives or when some window crosses `resetsAt`. The daemon sends instants (`resetsAt`, `runoutAt`) and kinds (`noteKind`, `paceInlineKind`), never time phrases; live countdowns ("Resets in…", "Out in…") are formatted in QML by `Model.describeLine` + `service.nowMs`, identically in the panel, the bar tooltip and Copy Summary.
7. **Python 3 stdlib only** — do not add external `pip` dependencies to `backend/` or to the `bin/omameter` CLI.
8. **Token renewal only through the engine** — OAuth providers declare `OAUTH = OAuthRefresh(...)` and read via `authorized(...)` / `with_fresh_token(...)` from [`backend/providers/session.py`](backend/providers/session.py); `resets.py` uses `fresh_credential` + `send_with_retry`. Do not reimplement refresh, the 401 retry or the `is_hub_account` renewal check inside a provider. Client IDs and token URLs live in the provider module; [`backend/auth.py`](backend/auth.py) imports them.
9. **Local token analytics is strictly opt-in & harness-direct** — `localTokenAnalytics` defaults to `false`; [`backend/analytics/parsers.py`](backend/analytics/parsers.py) reads only locally installed harnesses (Claude Code, GPT Codex, Antigravity, OpenCode) and must never depend on external services. `~/.t3` is never listed or read by default; the only exception is the `includeT3Antigravity` opt-in in `analytics.json` (default `false`, toggled by the "T3 Code" chip on the Antigravity row), which adds exactly `~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations` (`t3_antigravity_roots`). Without the opt-in only an existence check (`t3_antigravity_available`) runs, to offer the chip. Opting out deletes the events and scan state imported from that tree (`AnalyticsDB.forget_source_tree`). `set_local_enabled` returns immediately and `scan` streams `scan_progress` NDJSON events. On-demand LiteLLM pricing download uses `max_bytes=16 MiB` while keeping the `4 MiB` default safety cap for all other `HTTPClient` calls.

---

## 3. NDJSON Protocol (`OmameterService.qml` ↔ `backend/daemon.py`)

### Commands (QML → Daemon via stdin)
- Every command sent by the QML carries an `id` (unique string per QML process, generated by `Protocol.encode`). **Rule: a command with an `id` receives exactly one `result` event**, emitted after the work finishes (for pool commands — `refresh_*`, `add_api_key_account`, `add_hub`, `replace_hub_key`, `claim_reset`, `detect_local_credentials`, `import_local_credential`, `sync_hubs` — after the toasts/state that the work emits; `refresh_all` answers when all scheduled refreshes finish). A command without an `id` gets no `result`. An unknown command with an `id` → `ok: false`. In the daemon, `handle_command` → `_COMMANDS` (route + pool/inline) → `_run`, which catches exceptions (toast + `ok: false`, the loop keeps going) and emits the `result`; handlers return `None` (ok), a failure message (via `_fail`, which shows the same toast as before) or `AfterAll(futures)`.
- `start_auth` answers `ok` once the session has started (the rest follows via `auth_update`); `submit_auth_code` answers `ok` when a session was waiting for the code.
- With the daemon down, `OmameterService` fails the command immediately (outcome `failed`, clear message) and starts the daemon; when the process exits, `Protocol.daemonDown` fails all pending commands.
- State & refresh: `get_state`, `refresh_all`, `refresh_account`
- Layout & settings: `update_settings`, `update_proxy`, `set_metric_pinned`, `set_metric_hidden`, `set_provider_unified`, `move_provider`, `move_metric`, `undo_layout`
- Language: `set_language` (`{"cmd":"set_language","language":"pt-BR"}`; inline; re-emits `state` only when the language actually changed). Sent by `OmameterService.qml` when the daemon becomes ready and whenever Omarchy's language changes.
- Accounts & auth: `rename_account`, `remove_account`, `start_auth`, `submit_auth_code`, `cancel_auth`, `add_api_key_account`
- Local detection: `detect_local_credentials`, `import_local_credential` (`candidateId`)
- CLIProxyAPI Hubs: `add_hub`, `replace_hub_key`, `remove_hub`, `sync_hubs`
- Extra actions: `claim_reset`, `copy_text`

### Events (Daemon → QML via stdout)
- `{"type": "state", "dashboard": {...}, "accounts": [...], "hubs": [...], "settings": {...}, "layout": {...}, "proxy": {...}, "canUndo": bool, "nextRefreshAt": iso, "refreshing": bool, "authSession": {...}|null, "providerCatalog": [...]}`
  - Each `accounts` item carries `error` (redacted message of the last failure, `""` when none) and `errorKind` (the `ProviderError` kind: `auth_expired` | `api_key_rejected` | `rate_limited` | `offline` | `unreachable` | `refused` | `no_quota`; `""` when none). The QML does not consume `errorKind` yet.
  - Important `dashboard` fields: `hiddenMetrics` (list of hidden metrics, for restoring in Settings), `bar.allItems` (full list for scroll-cycling on the bar).
  - Each line (`allUsage[].accounts[].lines[]`, `memberRows[]`, `priority` lines) carries data, not time phrases: `resetsAt` (ISO), `runoutAt` (ISO instant when the line runs out at the current pace, only when before the reset; `""` otherwise), `paceInlineKind` (`"outIn"` = countdown to `runoutAt` | `"pace"` = show `paceTooltip` | `""`), `isAggregateName` (aggregate "N Accounts" name of a unified account; privacy does not hide it), `titleNamesWindow` (bool, computed from the untranslated title by `backend/models.py: title_names_window`; consumed by `Model.metricPickerLabel`), plus `meterState`, `amountText`, `trayValue`, `fillFraction`, `paceMark`, `paceTooltip`, `subtitle`, `severity`, `isBounded`, `lineType`, `copyIds`.
  - Metric titles, value labels/subtitles, provider notices and reset unavailable reasons arrive already translated into the active language (`backend/i18n.py` `metric_title`/`unit_label`/`data_text`).
  - `priority` lines add `priorityReason`, `prioritySeverity` and `noteKind` (`"amount"` empty balance → `amountText` | `"resetCompact"` exhausted → compact duration | `"reset"` follows `resetDisplayMode` | `"resetRelative"` always a countdown | `"outIn"` countdown to `runoutAt` | `""`).
  - `bar.items`/`bar.allItems` items carry `resetsAt` (no longer `resetText`); the `status()` IPC of `OmameterService.qml` returns the bar with `resetText` written live by `describeLine`.
  - Sentinels became fields: accounts carry `planMultiple` (unified with different plans; `plan` is `""`), breakdown models carry `isOther`, links (`links[]` of cards, accounts and `providerCatalog`) carry a stable `id` (`statusPage` | `usageDashboard` | `apiKeys` | `credits` | `orgBilling`, i18n `card.<id>`). Each non-unified account in `allUsage[].accounts[]` uses the same `id` as in `accounts[]`; the unified one uses `<kind>.unified`.
  - Removed (nobody read them): `resetText`, `compactResetText`, `priorityNote`, `paceInline`, `prioritySection`, `metricKind`, `isSpent`, `used`, `limit`, `remainingFraction`, `expiriesAt` on lines; `generatedAt` on `dashboard`; `refreshedAt` on `allUsage` accounts; `supportsCliProxy` on `providerCatalog`.
  - `tests/fixtures/dashboard.json` is generated by the real builder (`python3 tests/fixtures/make_dashboard_fixture.py`) and read by the `Model.js` tests; `tests/test_dashboard_fixture.py` fails if it diverges. Regenerate it when the payload changes.
- `{"type": "auth_update", "session": {...}|null}`
- `{"type": "local_credentials", "items": [...]}`
- `{"type": "toast", "message": str, "error": bool}` (user-facing message, already translated by the daemon into the active language)
- `{"type": "result", "id": str, "ok": bool, "error": str}` (machine channel; `error` only when `ok=false`, already translated into the active language and redacted with `redact_secrets`; never carries secrets)

---

## 4. Mandatory Validation & Deploy Checklist

Always run this full block before wrapping up any change:

```bash
# 1. Python unit tests (about 222 tests)
python3 -m unittest discover -s tests -v

# 2. JS unit tests (Model.js + Navigation.js + Settings.js + Protocol.js)
node --test tests/*.mjs

# 3. i18n catalogs + JSON validity (also run by tests/test_i18n.py and CI)
python3 scripts/check_i18n.py

# 4. QML lint against the Omarchy modules
/usr/bin/qmllint -I /usr/share/omarchy/shell OmameterService.qml OmameterWidget.qml ui/*.qml analytics/*.qml

# 5. Official Omarchy plugin validator
omarchy plugin validate .

# 6. Reinstall and reload omarchy-shell
./install.sh

# 7. Check live status + no plugin warnings in the journal
omarchy-shell paulimfavarato.omameter status
omarchy-shell paulimfavarato.omameter toggle && sleep 1 && omarchy-shell paulimfavarato.omameter toggle
journalctl --user --since "15 seconds ago" --no-pager | grep -iE "paulimfavarato\.omameter|ai-omameter|omameter/" | grep -v qmlscanner
```

---

## 5. Optional Next Improvements (Curated Backlog)

Critically evaluated items left as optional in case the user wants to evolve the plugin in future sessions:
1. **Quick provider filter in [`ui/AllUsageTab.qml`](ui/AllUsageTab.qml)**: compact icon bar (`All | Claude | ChatGPT | ...`) shown only when there are 3+ accounts, keeping `All` as the default.
2. **Optional reference budget (`display_limit`)** in the account detail for prepaid providers without a denominator (OpenRouter, Amp one-off credits), enabling a percentage bar and alerts.
3. **Read-only `omameter mcp` subcommand** in [`bin/omameter`](bin/omameter) (stdio JSON-RPC over the local cache, no network and no vault access).
4. **Guided visual test of new light/dark themes** in Omarchy to tune the contrast of the pace text (`paceInlineKind`/`runoutAt`) and of the `Pill`s when needed.
