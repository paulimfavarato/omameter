"""CredentialVault & HubKeyVault backed by libsecret (secret-tool) with 0600 local fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "omameter"
FALLBACK_VAULT_PATH = DATA_DIR / "vault.json"


class CredentialVault:
    """Stores one JSON credential blob per account or hub without touching other tools' files."""

    SERVICE_ACCOUNTS = "com.omarchy.omameter.accounts"
    SERVICE_HUBS = "com.omarchy.omameter.cliproxy-hubs"

    def __init__(self, fallback_path: Path = FALLBACK_VAULT_PATH, use_secret_tool: Optional[bool] = None) -> None:
        self.fallback_path = fallback_path
        if use_secret_tool is None:
            self._has_secret_tool = shutil.which("secret-tool") is not None
        else:
            self._has_secret_tool = use_secret_tool

    def _secret_tool_lookup(self, service: str, key: str) -> Optional[str]:
        if not self._has_secret_tool:
            return None
        try:
            proc = subprocess.run(
                ["secret-tool", "lookup", "service", service, "account", key],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
        except Exception:
            pass
        return None

    def _secret_tool_store(self, service: str, key: str, label: str, value: str) -> bool:
        if not self._has_secret_tool:
            return False
        try:
            proc = subprocess.run(
                ["secret-tool", "store", f"--label={label}", "service", service, "account", key],
                input=value,
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _secret_tool_clear(self, service: str, key: str) -> None:
        if not self._has_secret_tool:
            return
        try:
            subprocess.run(
                ["secret-tool", "clear", "service", service, "account", key],
                capture_output=True,
                timeout=3.0,
                check=False,
            )
        except Exception:
            pass

    def _load_fallback(self) -> Dict[str, Dict[str, Any]]:
        if not self.fallback_path.exists():
            return {}
        try:
            data = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_fallback(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.fallback_path.parent, 0o700)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.fallback_path.parent),
            prefix=f".{self.fallback_path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True))
            os.chmod(tmp_path, 0o600)
            tmp_path.replace(self.fallback_path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def get_account_credential(self, account_id: str) -> Optional[Dict[str, Any]]:
        raw = self._secret_tool_lookup(self.SERVICE_ACCOUNTS, account_id)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        data = self._load_fallback()
        entry = data.get("accounts", {}).get(account_id)
        return dict(entry) if isinstance(entry, dict) else None

    def set_account_credential(self, account_id: str, credential: Dict[str, Any]) -> None:
        encoded = json.dumps(credential, sort_keys=True)
        stored = self._secret_tool_store(
            self.SERVICE_ACCOUNTS,
            account_id,
            f"Omameter Account ({account_id[:8]})",
            encoded,
        )
        if not stored:
            data = self._load_fallback()
            data.setdefault("accounts", {})[account_id] = credential
            self._save_fallback(data)
        else:
            # Remove stale fallback copy if migrated to keyring
            data = self._load_fallback()
            if account_id in data.get("accounts", {}):
                del data["accounts"][account_id]
                self._save_fallback(data)

    def delete_account_credential(self, account_id: str) -> None:
        self._secret_tool_clear(self.SERVICE_ACCOUNTS, account_id)
        data = self._load_fallback()
        if account_id in data.get("accounts", {}):
            del data["accounts"][account_id]
            self._save_fallback(data)

    def get_hub_key(self, hub_id: str) -> Optional[Dict[str, str]]:
        raw = self._secret_tool_lookup(self.SERVICE_HUBS, hub_id)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                pass
        data = self._load_fallback()
        entry = data.get("hubs", {}).get(hub_id)
        return dict(entry) if isinstance(entry, dict) else None

    def set_hub_key(self, hub_id: str, base_url: str, management_key: str) -> None:
        payload = {"baseUrl": base_url, "managementKey": management_key}
        encoded = json.dumps(payload, sort_keys=True)
        stored = self._secret_tool_store(
            self.SERVICE_HUBS,
            hub_id,
            f"Omameter CLIProxyAPI Hub ({hub_id[:8]})",
            encoded,
        )
        if not stored:
            data = self._load_fallback()
            data.setdefault("hubs", {})[hub_id] = payload
            self._save_fallback(data)
        else:
            data = self._load_fallback()
            if hub_id in data.get("hubs", {}):
                del data["hubs"][hub_id]
                self._save_fallback(data)

    def delete_hub_key(self, hub_id: str) -> None:
        self._secret_tool_clear(self.SERVICE_HUBS, hub_id)
        data = self._load_fallback()
        if hub_id in data.get("hubs", {}):
            del data["hubs"][hub_id]
            self._save_fallback(data)
