# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Omameter, please **do not open a public GitHub issue**.

Instead, report it privately using **GitHub Security Advisories** ("Report a vulnerability" under the **Security** tab of this repository).

Please include as much of the following as you can:

- A clear description of the issue and its impact
- Steps to reproduce or a minimal proof of concept
- Affected files, versions, or commit SHA
- Any suggested mitigation or fix

You can expect an initial acknowledgement within a few days, followed by a coordinated fix and disclosure once verified.

---

## Security Architecture

Omameter is a local desktop plugin for `omarchy-shell` with a Python 3 standard-library backend daemon:

- **Credential isolation**: OAuth tokens, API keys, and CLIProxyAPI management keys stay exclusively inside the Python backend. The QML frontend only receives sanitized metadata and usage snapshots over stdio NDJSON.
- **Secret storage**: Credentials are stored in the system keyring via `secret-tool` (libsecret). When no keyring is available, the fallback vault (`~/.local/share/omameter/vault.json`) and local state/proxy files are created atomically with `0600` permissions inside `0700` directories.
- **Opt-in local credential detection**: Scanning local CLI logins (`claude`, `codex`, `gh`) and environment variables only runs when the user explicitly clicks *Detect Local Logins*. Tokens from `gh` are only read when the user explicitly imports that entry.
- **HTTP transport hardening**:
  - Only `http://` and `https://` URLs are permitted (no `file://`, `ftp://`, or `data:` handlers).
  - Redirects block `https://` → `http://` downgrades and strip sensitive headers (`Authorization`, `Cookie`, `Proxy-Authorization`, `X-Api-Key`) across origins.
  - When `curl` is used as a transport fallback, URLs, proxy settings, and headers are passed via a private `-K` config stream (`/dev/fd/<n>` or `0600` temporary file) rather than command-line arguments.
  - Error messages and logs pass through `redact_secrets()` before reaching UI toasts or stderr.
- **OAuth loopback listener**: Loopback OAuth callbacks bind both `127.0.0.1` and `::1` for `localhost` (failing closed if `::1` is already occupied by another process), verify `state` with constant-time comparison, and send `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
- **CLIProxyAPI hub keys**: Hub management keys are bound to the hub's saved `baseUrl` in the vault and will not be sent if the hub address changes.

---

## Public OAuth Client Identifiers (Expected in Source)

This repository intentionally contains public OAuth 2.0 client identifiers used by desktop/installed applications (such as Claude, Codex/ChatGPT, GitHub Copilot, Grok, Muse Code, and Google/Antigravity):

- Under [RFC 8252 (OAuth 2.0 for Native Apps)](https://datatracker.ietf.org/doc/html/rfc8252#section-8.5), native/desktop clients are **public clients** and cannot keep embedded client secrets confidential.
- Google's OAuth 2.0 flow for Desktop/Installed Apps still requires sending both `client_id` and `client_secret` (`ANTIGRAVITY_CLIENT_ID` and `ANTIGRAVITY_CLIENT_SECRET` in `backend/providers/antigravity.py`) at token exchange time even for public installed-app clients. Per [Google's OAuth documentation for Installed Apps](https://developers.google.com/identity/protocols/oauth2/native-app), *"The client secret is obviously not treated as a secret"* in this flow.
- These values do **not** grant access to any user data on their own; every sign-in still requires interactive user consent in the browser and PKCE / state validation.

Because they are public installed-app identifiers rather than private credentials, automated secret scanners may flag `ANTIGRAVITY_CLIENT_SECRET` as a false positive. Such alerts do not represent a leaked private secret.
