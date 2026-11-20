"""Opt-in, strictly local (no-network) detection of existing CLI & env AI credentials."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.auth import ACCOUNT_IDENTITY_KEY, decode_jwt_payload, jwt_expiry_iso
from backend.i18n import tr
from backend.models import (
    Account,
    PROVIDER_CATALOG,
    api_key_identity,
    format_iso8601,
    parse_iso8601,
)


ENV_API_KEY_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("env:OPENROUTER_API_KEY", "openrouter", "OPENROUTER_API_KEY"),
    ("env:OLLAMA_API_KEY", "ollama", "OLLAMA_API_KEY"),
    ("env:ZAI_API_KEY", "zai", "ZAI_API_KEY"),
    ("env:ZHIPU_API_KEY", "zai", "ZHIPU_API_KEY"),
    ("env:OPENCODE_API_KEY", "opencode", "OPENCODE_API_KEY"),
)


@dataclass(frozen=True)
class _DetectedCandidate:
    id: str
    kind: str
    source: str
    method: str
    label: str
    credential: Dict[str, Any]
    identity: str
    email: Optional[str] = None
    account_id_hint: Optional[str] = None


def _parse_expires_at(raw: Any) -> Optional[str]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return format_iso8601(parse_iso8601(raw))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        dt = parse_iso8601(text)
        if dt is not None:
            return format_iso8601(dt)
        try:
            num = float(text)
            if math.isfinite(num) and num > 0:
                return format_iso8601(parse_iso8601(num))
        except ValueError:
            pass
    return None


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _non_empty_str(val: Any) -> Optional[str]:
    if isinstance(val, str):
        cleaned = val.strip()
        if cleaned:
            return cleaned
    return None


def _detect_claude_cli(home: Path) -> Optional[_DetectedCandidate]:
    data = _read_json_file(home / ".claude" / ".credentials.json")
    if not data:
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None

    access_token = _non_empty_str(oauth.get("accessToken"))
    if not access_token:
        return None
    refresh_token = _non_empty_str(oauth.get("refreshToken"))
    expires_at = _parse_expires_at(oauth.get("expiresAt"))

    oauth_account = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
    email = (
        _non_empty_str(oauth.get("email"))
        or _non_empty_str(oauth.get("accountEmail"))
        or _non_empty_str(data.get("email"))
        or _non_empty_str(oauth_account.get("emailAddress"))
    )
    account_uuid = _non_empty_str(oauth.get("accountUuid")) or _non_empty_str(oauth_account.get("accountUuid"))
    org_uuid = _non_empty_str(oauth.get("organizationUuid")) or _non_empty_str(oauth_account.get("organizationUuid"))

    if account_uuid and org_uuid:
        identity = f"{account_uuid}/{org_uuid}"
    elif account_uuid:
        identity = account_uuid
    elif email:
        identity = email.lower()
    else:
        identity = api_key_identity(access_token)

    extras: Dict[str, str] = {}
    if identity:
        extras[ACCOUNT_IDENTITY_KEY] = identity

    credential: Dict[str, Any] = {
        "type": "oauth",
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "extras": extras,
    }
    return _DetectedCandidate(
        id="claude:cli",
        kind="claude",
        source="Claude Code CLI",
        method="oauth",
        label=email or "Claude Code CLI",
        credential=credential,
        identity=identity,
        email=email,
        account_id_hint=account_uuid,
    )


def _detect_codex_cli(home: Path) -> Optional[_DetectedCandidate]:
    data = _read_json_file(home / ".codex" / "auth.json")
    if not data:
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None

    access_token = _non_empty_str(tokens.get("access_token"))
    if not access_token:
        return None
    refresh_token = _non_empty_str(tokens.get("refresh_token"))
    id_token = _non_empty_str(tokens.get("id_token")) or ""

    access_claims = decode_jwt_payload(access_token) or {}
    id_claims = decode_jwt_payload(id_token) or {}

    access_auth = access_claims.get("https://api.openai.com/auth")
    id_auth = id_claims.get("https://api.openai.com/auth")
    account_id = (
        _non_empty_str(tokens.get("account_id"))
        or (_non_empty_str(access_auth.get("chatgpt_account_id")) if isinstance(access_auth, dict) else None)
        or (_non_empty_str(id_auth.get("chatgpt_account_id")) if isinstance(id_auth, dict) else None)
    )
    email = _non_empty_str(id_claims.get("email")) or _non_empty_str(access_claims.get("email"))
    expires_at = jwt_expiry_iso(access_token)

    extras: Dict[str, str] = {}
    if account_id:
        extras["chatgpt_account_id"] = account_id

    if account_id and email:
        identity = f"{account_id}/{email}"
    elif account_id:
        identity = account_id
    elif email:
        identity = email.lower()
    else:
        identity = api_key_identity(access_token)

    if identity:
        extras[ACCOUNT_IDENTITY_KEY] = identity

    credential: Dict[str, Any] = {
        "type": "oauth",
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "extras": extras,
    }
    return _DetectedCandidate(
        id="codex:cli",
        kind="codex",
        source="Codex CLI",
        method="oauth",
        label=email or "Codex CLI",
        credential=credential,
        identity=identity,
        email=email,
        account_id_hint=account_id,
    )


def _detect_copilot_gh() -> Optional[_DetectedCandidate]:
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return None
    try:
        proc = subprocess.run(
            [gh_bin, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    token = (proc.stdout or "").strip()
    if not token:
        return None

    identity = api_key_identity(token)
    credential: Dict[str, Any] = {
        "type": "oauth",
        "accessToken": token,
        "refreshToken": None,
        "expiresAt": None,
        "extras": {ACCOUNT_IDENTITY_KEY: identity},
    }
    return _DetectedCandidate(
        id="copilot:gh",
        kind="copilot",
        source="GitHub CLI",
        method="oauth",
        label="GitHub CLI",
        credential=credential,
        identity=identity,
    )


def _detect_env_api_keys(env: Mapping[str, str]) -> List[_DetectedCandidate]:
    found: List[_DetectedCandidate] = []
    seen_identities: set[Tuple[str, str]] = set()
    for cid, kind, var_name in ENV_API_KEY_SPECS:
        raw = env.get(var_name)
        val = _non_empty_str(raw)
        if not val:
            continue
        identity = api_key_identity(val)
        if (kind, identity) in seen_identities:
            continue
        seen_identities.add((kind, identity))
        spec = PROVIDER_CATALOG[kind]
        credential: Dict[str, Any] = {
            "type": "api_key",
            "apiKey": val,
            "identity": identity,
        }
        found.append(
            _DetectedCandidate(
                id=cid,
                kind=kind,
                source=var_name,
                method="api_key",
                label=f"{spec.display_name} ({var_name})",
                credential=credential,
                identity=identity,
            )
        )
    return found


def _collect_candidates(
    *,
    home_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    check_gh: bool = True,
) -> List[_DetectedCandidate]:
    home = home_dir if home_dir is not None else Path.home()
    env = environ if environ is not None else os.environ

    candidates: List[_DetectedCandidate] = []
    claude = _detect_claude_cli(home)
    if claude is not None:
        candidates.append(claude)
    codex = _detect_codex_cli(home)
    if codex is not None:
        candidates.append(codex)
    if check_gh:
        copilot = _detect_copilot_gh()
        if copilot is not None:
            candidates.append(copilot)
    candidates.extend(_detect_env_api_keys(env))
    return candidates


def _is_already_added(existing_accounts: List[Account], cand: _DetectedCandidate) -> bool:
    email_low = cand.email.strip().lower() if cand.email else None
    for acc in existing_accounts:
        if acc.kind != cand.kind:
            continue
        if cand.identity and acc.identity and acc.identity == cand.identity:
            return True
        if email_low:
            if acc.label and acc.label.strip().lower() == email_low:
                return True
            if acc.identity and email_low in acc.identity.lower():
                return True
        if cand.account_id_hint and acc.identity and cand.account_id_hint in acc.identity:
            return True
    return False


def scan_local_credentials(
    existing_accounts: List[Account],
    *,
    home_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    check_gh: bool = True,
) -> List[Dict[str, Any]]:
    """Scan local CLI credential files and env vars; return ONLY safe metadata (never raw secrets)."""
    items: List[Dict[str, Any]] = []
    for cand in _collect_candidates(home_dir=home_dir, environ=environ, check_gh=check_gh):
        spec = PROVIDER_CATALOG[cand.kind]
        items.append({
            "id": cand.id,
            "kind": cand.kind,
            "displayName": spec.display_name,
            "icon": spec.icon,
            "source": cand.source,
            "method": cand.method,
            "label": cand.label,
            "alreadyAdded": _is_already_added(existing_accounts, cand),
        })
    return items


def candidate_source_name(candidate_id: str) -> str:
    """Return a human-readable source name for a candidate ID."""
    if candidate_id == "claude:cli":
        return "Claude Code CLI"
    if candidate_id == "codex:cli":
        return "Codex CLI"
    if candidate_id == "copilot:gh":
        return "GitHub CLI"
    for cid, _, var_name in ENV_API_KEY_SPECS:
        if cid == candidate_id:
            return var_name
    return tr("local.fallbackSource", "local CLI")


def load_local_credential_payload(
    candidate_id: str,
    *,
    home_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str, str, Dict[str, Any], str]:
    """Load `(kind, method, label, credential_dict, identity)` for a detected local credential ID."""
    home = home_dir if home_dir is not None else Path.home()
    env = environ if environ is not None else os.environ

    cand: Optional[_DetectedCandidate] = None
    if candidate_id == "claude:cli":
        cand = _detect_claude_cli(home)
    elif candidate_id == "codex:cli":
        cand = _detect_codex_cli(home)
    elif candidate_id == "copilot:gh":
        cand = _detect_copilot_gh()
    else:
        for item in _detect_env_api_keys(env):
            if item.id == candidate_id:
                cand = item
                break

    if cand is None:
        raise ValueError(tr("local.credentialGone", "Local credential '%1' is no longer available.", candidate_id))

    return cand.kind, cand.method, cand.label, cand.credential, cand.identity
