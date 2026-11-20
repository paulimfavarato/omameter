"""HTTP/HTTPS/SOCKS5 client with ProxyConfig (~/.config/omameter/proxy.json), strict bounds, and log redaction."""

from __future__ import annotations

import functools
import json
import os
import re
import ssl
import subprocess
import tempfile
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

_SENSITIVE_REDIRECT_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})

# (pattern, replacement) applied in order by redact_secrets()
_SECRET_RULES = (
    (re.compile(r"(Authorization\s*:\s*(?:Bearer|Token|Basic)\s+)[^\s'\",;]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(devin-session-token\$)[^\s'\",;]+"), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+/=$]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(WorkosCursorSessionToken=)[^;\s'\"]+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"([?&](?:verifier|code_verifier|access_token|refresh_token|api_key|token|client_secret|management_key)=)[^&\s'\"]+",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"sk-[A-Za-z0-9\-_]{10,}"), "[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{10,}"), "[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED]"),
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^:@/\s]+:)([^@/\s]+)(@)"), r"\1[REDACTED]\3"),
)


def redact_secrets(text: str) -> str:
    if not text:
        return ""
    out = str(text)
    for pat, repl in _SECRET_RULES:
        out = pat.sub(repl, out)
    return out


def _origin_key(url: str) -> Tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    default_port = 443 if scheme == "https" else 80
    try:
        port = parts.port or default_port
    except ValueError:
        port = default_port
    return scheme, host, port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow HTTP(S) redirects without leaking credentials cross-origin or downgrading TLS."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        old_parts = urllib.parse.urlsplit(req.full_url)
        new_parts = urllib.parse.urlsplit(newurl)
        old_scheme = (old_parts.scheme or "").lower()
        new_scheme = (new_parts.scheme or "").lower()

        if new_scheme not in ("http", "https"):
            raise urllib.error.HTTPError(
                newurl, code, f"{msg} - Redirection to non-HTTP(S) scheme is not allowed", headers, fp
            )
        if old_scheme == "https" and new_scheme != "https":
            raise urllib.error.HTTPError(
                newurl, code, f"{msg} - HTTPS to HTTP redirect downgrade is not allowed", headers, fp
            )

        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None

        if _origin_key(req.full_url) != _origin_key(newurl):
            for hdr_map in (new_req.headers, new_req.unredirected_hdrs):
                for key in list(hdr_map.keys()):
                    if key.lower() in _SENSITIVE_REDIRECT_HEADERS:
                        del hdr_map[key]
        return new_req


def _curl_quote(value: str) -> str:
    text = str(value)
    if "\r" in text or "\n" in text or "\x00" in text:
        raise HTTPTransportError(tr("net.httpError", "HTTP error: %1", "invalid control character in request"))
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_private_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
        _write_private_json(path, existing)
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
        parsed_scheme = (urllib.parse.urlsplit(url).scheme or "").lower()
        if parsed_scheme not in ("http", "https"):
            raise HTTPTransportError(tr("net.httpError", "HTTP error: %1", f"unsupported URL scheme: {parsed_scheme}"))

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

    def _build_urllib_opener(self) -> urllib.request.OpenerDirector:
        opener = urllib.request.OpenerDirector()
        if self.proxy.enabled and self.proxy.url:
            opener.add_handler(urllib.request.ProxyHandler({"http": self.proxy.url, "https": self.proxy.url}))
        else:
            opener.add_handler(urllib.request.ProxyHandler())
        opener.add_handler(urllib.request.HTTPHandler())
        opener.add_handler(urllib.request.HTTPSHandler(context=self._ssl_ctx))
        opener.add_handler(_SafeRedirectHandler())
        opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(urllib.request.UnknownHandler())
        return opener

    def _request_urllib(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HTTPResponse:
        opener = self._build_urllib_opener()
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
        config_lines = [f"url = {_curl_quote(url)}"]
        if self.proxy.enabled and self.proxy.url:
            config_lines.append(f"proxy = {_curl_quote(self.proxy.url)}")
        for k, v in headers.items():
            config_lines.append(f"header = {_curl_quote(f'{k}: {v}')}")
        config_bytes = ("\n".join(config_lines) + "\n").encode("utf-8")

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
        if body is not None:
            cmd.extend(["--data-binary", "@-"])

        pipe_r: Optional[int] = None
        tmp_cfg: Optional[Path] = None
        pass_fds: Tuple[int, ...] = ()
        try:
            if len(config_bytes) <= 32768 and os.path.exists("/dev/fd"):
                pipe_r, pipe_w = os.pipe()
                try:
                    os.write(pipe_w, config_bytes)
                finally:
                    os.close(pipe_w)
                pass_fds = (pipe_r,)
                cmd.extend(["-K", f"/dev/fd/{pipe_r}"])
            else:
                fd, tmp_name = tempfile.mkstemp(prefix=".omameter-curl-", suffix=".cfg")
                tmp_cfg = Path(tmp_name)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(config_bytes)
                os.chmod(tmp_cfg, 0o600)
                cmd.extend(["-K", str(tmp_cfg)])

            proc = subprocess.run(
                cmd,
                input=body,
                capture_output=True,
                timeout=timeout + 5.0,
                check=False,
                pass_fds=pass_fds,
            )
        except subprocess.TimeoutExpired as err:
            raise HTTPTransportError(tr("net.timedOut", "Request timed out after %1s", int(timeout)), timeout=True) from err
        except OSError as err:
            raise HTTPTransportError(tr("net.curlUnavailable", "curl unavailable: %1", err), offline=True) from err
        finally:
            if pipe_r is not None:
                try:
                    os.close(pipe_r)
                except OSError:
                    pass
            if tmp_cfg is not None:
                try:
                    tmp_cfg.unlink(missing_ok=True)
                except OSError:
                    pass

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
