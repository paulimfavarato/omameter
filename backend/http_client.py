"""HTTP/HTTPS/SOCKS5 client with ProxyConfig (~/.config/omameter/proxy.json), strict bounds, and log redaction."""

from __future__ import annotations

import functools
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from backend.i18n import tr


MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap
DEFAULT_TIMEOUT_SEC = 25.0
OMAMETER_CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "omameter" / "proxy.json"


_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+/=]+", re.IGNORECASE),
    re.compile(r"(sk-[A-Za-z0-9\-_]{10,})"),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{10,})"),
    re.compile(r"(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,})"),
    re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^:@/\s]+:)([^@/\s]+)(@)"),
]


def redact_secrets(text: str) -> str:
    if not text:
        return ""
    out = str(text)
    out = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", out)
    for pat in _SECRET_PATTERNS[1:4]:
        out = pat.sub("[REDACTED]", out)
    out = _SECRET_PATTERNS[4].sub(r"\1[REDACTED]\3", out)
    return out


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool = False
    url: str = ""

    DEFAULT_PORTS = {"socks5": 1080, "http": 80, "https": 443}

    def redacted_url(self) -> str:
        return redact_secrets(self.url)

    @classmethod
    def validate_url(cls, raw_url: str) -> Tuple[bool, Optional[str]]:
        text = (raw_url or "").strip()
        if not text:
            return False, tr("net.proxyUrlEmpty", "Proxy URL is empty.")
        parsed = urllib.parse.urlsplit(text)
        scheme = (parsed.scheme or "").lower()
        if scheme not in cls.DEFAULT_PORTS:
            return False, tr("net.proxySchemeUnsupported", "Unsupported proxy scheme (use socks5, http, or https).")
        if not parsed.hostname:
            return False, tr("net.proxyHostMissing", "Proxy host is missing.")
        try:
            port = parsed.port or cls.DEFAULT_PORTS[scheme]
        except ValueError:
            return False, tr("net.proxyPortInvalid", "Proxy port is invalid.")
        if port <= 0 or port > 65535:
            return False, tr("net.proxyPortOutOfRange", "Proxy port is out of range.")
        return True, None

    @classmethod
    def load(cls, path: Path = OMAMETER_CONFIG_PATH) -> "ProxyConfig":
        if not path.exists():
            return cls(enabled=False, url="")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            proxy = raw.get("proxy") if isinstance(raw, dict) else None
            if not isinstance(proxy, dict):
                return cls(enabled=False, url="")
            enabled = bool(proxy.get("enabled", False))
            url = str(proxy.get("url") or "").strip()
            if enabled:
                ok, _ = cls.validate_url(url)
                if not ok:
                    return cls(enabled=False, url="")
            return cls(enabled=enabled, url=url)
        except Exception:
            return cls(enabled=False, url="")

    @classmethod
    def save(cls, enabled: bool, url: str, path: Path = OMAMETER_CONFIG_PATH) -> Tuple[bool, Optional[str]]:
        clean_url = (url or "").strip()
        if enabled:
            ok, err = cls.validate_url(clean_url)
            if not ok:
                return False, err
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        existing["proxy"] = {"enabled": bool(enabled), "url": clean_url}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return True, None


@dataclass
class HTTPResponse:
    status: int
    headers: Dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        target = name.lower()
        for k, v in self.headers.items():
            if k.lower() == target:
                return v
        return default


class HTTPTransportError(Exception):
    def __init__(self, message: str, *, offline: bool = False, timeout: bool = False) -> None:
        super().__init__(redact_secrets(message))
        self.offline = offline
        self.timeout = timeout


@functools.lru_cache(maxsize=1)
def _default_ssl_context() -> ssl.SSLContext:
    """One shared CA-loaded context; building it parses the system CA store."""
    return ssl.create_default_context()


class HTTPClient:
    """HTTP client supporting urllib and curl (for SOCKS5 proxies & HTTP/2 endpoints)."""

    def __init__(self, proxy: Optional[ProxyConfig] = None, timeout: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.proxy = proxy or ProxyConfig.load()
        self.timeout = timeout
        self._ssl_ctx = _default_ssl_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        json_body: Any = None,
        form_body: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        use_curl: bool = False,
    ) -> HTTPResponse:
        req_headers = dict(headers or {})
        if not any(k.lower() == "user-agent" for k in req_headers):
            req_headers["User-Agent"] = "Omameter/1.0"
        payload = body
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            payload = urllib.parse.urlencode(form_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        eff_timeout = timeout or self.timeout
        needs_curl = use_curl or (self.proxy.enabled and self.proxy.url.lower().startswith("socks5://"))
        if needs_curl:
            return self._request_curl(method, url, req_headers, payload, eff_timeout)
        return self._request_urllib(method, url, req_headers, payload, eff_timeout)

    def get(self, url: str, **kwargs: Any) -> HTTPResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HTTPResponse:
        return self.request("POST", url, **kwargs)

    def _request_urllib(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HTTPResponse:
        handlers: list[Any] = [urllib.request.HTTPSHandler(context=self._ssl_ctx)]
        if self.proxy.enabled and self.proxy.url:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy.url, "https": self.proxy.url}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise HTTPTransportError(tr("net.responseTooLarge", "Response exceeded 4 MiB safety limit"))
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return HTTPResponse(status=int(resp.status), headers=resp_headers, body=raw)
        except urllib.error.HTTPError as err:
            raw = err.read(MAX_RESPONSE_BYTES) if err.fp else b""
            resp_headers = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
            return HTTPResponse(status=int(err.code), headers=resp_headers, body=raw)
        except urllib.error.URLError as err:
            reason = str(err.reason)
            is_timeout = "timed out" in reason.lower()
            raise HTTPTransportError(tr("net.networkError", "Network error: %1", reason), offline=not is_timeout, timeout=is_timeout) from err
        except TimeoutError as err:
            raise HTTPTransportError(tr("net.timedOut", "Request timed out after %1s", int(timeout)), timeout=True) from err
        except Exception as err:
            raise HTTPTransportError(tr("net.httpError", "HTTP error: %1", err), offline=True) from err

    def _request_curl(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HTTPResponse:
        cmd = [
            "curl",
            "-sS",
            "--max-time",
            str(max(1, int(round(timeout)))),
            "--max-filesize",
            str(MAX_RESPONSE_BYTES),
            "-X",
            method.upper(),
            "-D",
            "-",
            "--no-progress-meter",
        ]
        if self.proxy.enabled and self.proxy.url:
            cmd.extend(["--proxy", self.proxy.url])
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if body is not None:
            cmd.extend(["--data-binary", "@-"])
        cmd.append(url)
        try:
            proc = subprocess.run(
                cmd,
                input=body,
                capture_output=True,
                timeout=timeout + 5.0,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise HTTPTransportError(tr("net.timedOut", "Request timed out after %1s", int(timeout)), timeout=True) from err
        except OSError as err:
            raise HTTPTransportError(tr("net.curlUnavailable", "curl unavailable: %1", err), offline=True) from err

        if proc.returncode != 0:
            err_msg = proc.stderr.decode("utf-8", errors="replace").strip() or tr("net.curlExited", "curl exited %1", proc.returncode)
            is_timeout = proc.returncode == 28
            raise HTTPTransportError(err_msg, offline=not is_timeout, timeout=is_timeout)

        raw = proc.stdout
        if len(raw) > MAX_RESPONSE_BYTES + 65536:
            raise HTTPTransportError(tr("net.responseOverLimit", "Response exceeded safety limit"))

        # Parse final HTTP header block (skipping 100-Continue or proxy CONNECT headers)
        sep = b"\r\n\r\n"
        pos = 0
        status_code = 0
        resp_headers: Dict[str, str] = {}
        while True:
            idx = raw.find(sep, pos)
            if idx == -1:
                body_bytes = raw[pos:]
                break
            block = raw[pos:idx].decode("iso-8859-1", errors="replace")
            lines = block.split("\r\n")
            if not lines or not lines[0].upper().startswith("HTTP/"):
                body_bytes = raw[pos:]
                break
            parts = lines[0].split(" ", 2)
            code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            hdrs: Dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            pos = idx + len(sep)
            status_code = code
            resp_headers = hdrs
            # Continue past interim 1xx or proxy 200 Connection Established
            if code == 100 or (code == 200 and "connection established" in lines[0].lower()):
                continue
            body_bytes = raw[pos:]
            break

        return HTTPResponse(status=status_code, headers=resp_headers, body=body_bytes[:MAX_RESPONSE_BYTES])
