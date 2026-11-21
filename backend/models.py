"""Core domain models, provider catalog, descriptors, and formatters."""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.i18n import tr

# PriorityLimits lives in backend.settings: its bounds come from the manifest schema.
from backend.settings import PriorityLimits  # noqa: F401


# ---------------------------------------------------------------------------
# Time helpers (UTC ISO-8601 with fractional seconds)
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_iso8601(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso8601(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        val = float(raw)
        if not math.isfinite(val) or val <= 0:
            return None
        # Distinguish seconds vs milliseconds
        if val > 1e12:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Metric periods (milliseconds)
# ---------------------------------------------------------------------------

class MetricPeriod:
    SESSION_MS = 5 * 3600 * 1000          # 18_000_000 (5h)
    DAY_MS = 24 * 3600 * 1000             # 86_400_000 (24h)
    WEEK_MS = 7 * 24 * 3600 * 1000        # 604_800_000 (7d)
    MONTH_MS = 30 * 24 * 3600 * 1000      # 2_592_000_000 (30d)


# ---------------------------------------------------------------------------
# Provider catalog (all 13 providers)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderLink:
    """External link on a provider card. `id` is the stable key the UI localizes; `title` is the English fallback."""
    id: str
    title: str
    url: str

    def to_dict(self) -> Dict[str, str]:
        return {"id": self.id, "title": self.title, "url": self.url}


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str
    auth_methods: Tuple[str, ...]  # "oauth", "device", "api_key", "org_token"
    icon: str
    links: Tuple[ProviderLink, ...]
    key_page: Optional[str] = None
    unifiable: bool = False
    supports_cliproxy: bool = False


PROVIDER_CATALOG: Dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        id="claude",
        display_name="Claude",
        auth_methods=("oauth",),
        icon="claude.svg",
        links=(
            ProviderLink("statusPage", "Status Page", "https://status.claude.com/"),
            ProviderLink("usageDashboard", "Usage Dashboard", "https://claude.ai/settings/usage"),
        ),
        unifiable=True,
        supports_cliproxy=True,
    ),
    "codex": ProviderSpec(
        id="codex",
        display_name="ChatGPT",
        auth_methods=("oauth", "device"),
        icon="codex.svg",
        links=(
            ProviderLink("statusPage", "Status Page", "https://status.openai.com/"),
            ProviderLink("usageDashboard", "Usage Dashboard", "https://chatgpt.com/settings/usage"),
        ),
        unifiable=True,
        supports_cliproxy=True,
    ),
    "cursor": ProviderSpec(
        id="cursor",
        display_name="Cursor",
        auth_methods=("oauth",),
        icon="cursor.svg",
        links=(
            ProviderLink("statusPage", "Status Page", "https://status.cursor.com/"),
            ProviderLink("usageDashboard", "Usage Dashboard", "https://www.cursor.com/dashboard"),
        ),
    ),
    "amp": ProviderSpec(
        id="amp",
        display_name="Amp",
        auth_methods=("device",),
        icon="amp.svg",
        links=(
            ProviderLink("usageDashboard", "Usage Dashboard", "https://ampcode.com/settings"),
        ),
    ),
    "antigravity": ProviderSpec(
        id="antigravity",
        display_name="Antigravity",
        auth_methods=("oauth",),
        icon="antigravity.svg",
        links=(),
        supports_cliproxy=True,
    ),
    "copilot": ProviderSpec(
        id="copilot",
        display_name="Copilot",
        auth_methods=("device", "org_token"),
        icon="copilot.svg",
        links=(
            ProviderLink("statusPage", "Status Page", "https://www.githubstatus.com/"),
            ProviderLink("usageDashboard", "Usage Dashboard", "https://github.com/settings/billing"),
        ),
    ),
    "devin": ProviderSpec(
        id="devin",
        display_name="Devin",
        auth_methods=("oauth",),
        icon="devin.svg",
        links=(
            ProviderLink("usageDashboard", "Usage Dashboard", "https://app.devin.ai/settings/plans"),
        ),
        supports_cliproxy=True,
    ),
    "grok": ProviderSpec(
        id="grok",
        display_name="Grok",
        auth_methods=("device",),
        icon="grok.svg",
        links=(
            ProviderLink("usageDashboard", "Usage Dashboard", "https://grok.com/?_s=usage"),
        ),
        supports_cliproxy=True,
    ),
    "muse": ProviderSpec(
        id="muse",
        display_name="Muse Code",
        auth_methods=("device",),
        icon="muse.svg",
        links=(
            ProviderLink("usageDashboard", "Usage Dashboard", "https://dev.meta.ai/usage"),
        ),
    ),
    "ollama": ProviderSpec(
        id="ollama",
        display_name="Ollama",
        auth_methods=("api_key",),
        icon="ollama.svg",
        key_page="https://ollama.com/settings/keys",
        links=(
            ProviderLink("usageDashboard", "Usage Dashboard", "https://ollama.com/settings"),
            ProviderLink("apiKeys", "API Keys", "https://ollama.com/settings/keys"),
        ),
    ),
    "opencode": ProviderSpec(
        id="opencode",
        display_name="OpenCode",
        auth_methods=("device", "api_key"),
        icon="opencode.svg",
        links=(
            ProviderLink("statusPage", "Status Page", "https://status.opencode.ai/"),
            ProviderLink("usageDashboard", "Usage Dashboard", "https://opencode.ai/console"),
        ),
    ),
    "openrouter": ProviderSpec(
        id="openrouter",
        display_name="OpenRouter",
        auth_methods=("api_key",),
        icon="openrouter.svg",
        key_page="https://openrouter.ai/settings/management-keys",
        links=(
            ProviderLink("statusPage", "Status Page", "https://status.openrouter.ai/"),
            ProviderLink("usageDashboard", "Usage Dashboard", "https://openrouter.ai/activity"),
            ProviderLink("credits", "Credits", "https://openrouter.ai/settings/credits"),
        ),
    ),
    "zai": ProviderSpec(
        id="zai",
        display_name="Z.ai",
        auth_methods=("api_key",),
        icon="zai.svg",
        key_page="https://z.ai/manage-apikey/apikey-list",
        links=(
            ProviderLink("usageDashboard", "Usage Dashboard", "https://z.ai/manage-apikey/coding-plan/personal/my-plan"),
            ProviderLink("apiKeys", "API Keys", "https://z.ai/manage-apikey/apikey-list"),
        ),
    ),
}

PROVIDER_ORDER_DEFAULT: Tuple[str, ...] = tuple(PROVIDER_CATALOG.keys())
UNIFIABLE_KINDS: Tuple[str, ...] = ("claude", "codex")


# ---------------------------------------------------------------------------
# Default account labels
#
# Every rule about "the label an account gets when nobody named it" lives here:
# `Account.create` (empty label -> bare display name), `default_account_label`
# (API-key accounts and hub mirrors -> "<display> Account") and
# `is_default_label` (which labels a discovered account name may replace).
# ---------------------------------------------------------------------------

def default_account_label(kind: str, label: Optional[str] = None) -> str:
    """`label` stripped, or "<display> Account" when it is empty or only the
    provider's display name (compared without case)."""
    display = PROVIDER_CATALOG[kind].display_name
    cleaned = (label or "").strip()
    if not cleaned or cleaned.lower() == display.lower():
        return f"{display} Account"
    return cleaned


def is_default_label(kind: str, label: str) -> bool:
    """True when `label` is still a placeholder that a discovered name may
    replace: "", the display name, or "<display> Account" (exact match)."""
    display = PROVIDER_CATALOG[kind].display_name
    return label in ("", display, f"{display} Account")


# ---------------------------------------------------------------------------
# Accounts & CLIProxyAPI Hubs
# ---------------------------------------------------------------------------

@dataclass
class Account:
    id: str
    kind: str
    label: str
    created_at: str = field(default_factory=lambda: format_iso8601(utc_now()) or "")
    organization: Optional[str] = None
    domain: Optional[str] = None
    identity: Optional[str] = None
    hub_id: Optional[str] = None
    hub_host: Optional[str] = None
    hub_auth_index: Optional[str] = None
    hub_file_name: Optional[str] = None
    auth_mode: Optional[str] = None  # "oauth", "device", "api_key", "org_token", "hub"

    @classmethod
    def create(
        cls,
        kind: str,
        label: str,
        *,
        organization: Optional[str] = None,
        domain: Optional[str] = None,
        identity: Optional[str] = None,
        hub_id: Optional[str] = None,
        hub_host: Optional[str] = None,
        hub_auth_index: Optional[str] = None,
        hub_file_name: Optional[str] = None,
        auth_mode: Optional[str] = None,
    ) -> "Account":
        return cls(
            id=str(uuid.uuid4()).lower(),
            kind=kind,
            # Unlike `default_account_label`, an empty label becomes the bare display name.
            label=label.strip() or PROVIDER_CATALOG[kind].display_name,
            organization=organization,
            domain=domain,
            identity=identity,
            hub_id=hub_id,
            hub_host=hub_host,
            hub_auth_index=hub_auth_index,
            hub_file_name=hub_file_name,
            auth_mode=auth_mode,
        )

    @property
    def card_id(self) -> str:
        prefix = self.id.replace("-", "").lower()[:8]
        return f"{self.kind}@{prefix}"

    @property
    def spec(self) -> ProviderSpec:
        return PROVIDER_CATALOG[self.kind]

    @property
    def display_name(self) -> str:
        return self.spec.display_name

    @property
    def account_name(self) -> str:
        return self.label.strip() if self.label.strip() else self.display_name

    @property
    def label_beside_name(self) -> Optional[str]:
        lbl = self.label.strip()
        if not lbl or lbl.lower() == self.display_name.lower():
            return None
        return lbl

    @property
    def full_name(self) -> str:
        beside = self.label_beside_name
        return f"{self.display_name} · {beside}" if beside else self.display_name

    @property
    def effective_auth_method(self) -> str:
        if self.hub_id:
            return "hub"
        if self.organization:
            return "org_token"
        if self.auth_mode:
            return self.auth_mode
        return self.spec.auth_methods[0]

    @property
    def is_hub_account(self) -> bool:
        return bool(self.hub_id)

    def links(self) -> List[Dict[str, str]]:
        import urllib.parse

        result = [lnk.to_dict() for lnk in self.spec.links]
        if self.kind == "copilot" and self.organization:
            host = self.domain or "github.com"
            org_slug = urllib.parse.quote(self.organization, safe="")
            result.append({
                "id": "orgBilling",
                "title": "Organization Billing",
                "url": f"https://{host}/organizations/{org_slug}/settings/billing",
            })
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "cardId": self.card_id,
            "kind": self.kind,
            "label": self.label,
            "createdAt": self.created_at,
            "organization": self.organization,
            "domain": self.domain,
            "identity": self.identity,
            "hubId": self.hub_id,
            "hubHost": self.hub_host,
            "hubAuthIndex": self.hub_auth_index,
            "hubFileName": self.hub_file_name,
            "authMode": self.auth_mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Account":
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            label=str(data.get("label") or ""),
            created_at=str(data.get("createdAt") or format_iso8601(utc_now()) or ""),
            organization=data.get("organization"),
            domain=data.get("domain"),
            identity=data.get("identity"),
            hub_id=data.get("hubId"),
            hub_host=data.get("hubHost"),
            hub_auth_index=data.get("hubAuthIndex"),
            hub_file_name=data.get("hubFileName"),
            auth_mode=data.get("authMode"),
        )


@dataclass
class CLIProxyHub:
    id: str
    base_url: str
    host: str
    created_at: str = field(default_factory=lambda: format_iso8601(utc_now()) or "")
    last_synced_at: Optional[str] = None
    last_error: Optional[str] = None
    unsupported_count: int = 0
    key_rejected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "baseUrl": self.base_url,
            "host": self.host,
            "createdAt": self.created_at,
            "lastSyncedAt": self.last_synced_at,
            "lastError": self.last_error,
            "unsupportedCount": self.unsupported_count,
            "keyRejected": self.key_rejected,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CLIProxyHub":
        return cls(
            id=str(data["id"]),
            base_url=str(data["baseUrl"]),
            host=str(data["host"]),
            created_at=str(data.get("createdAt") or ""),
            last_synced_at=data.get("lastSyncedAt"),
            last_error=data.get("lastError"),
            unsupported_count=int(data.get("unsupportedCount") or 0),
            key_rejected=bool(data.get("keyRejected", False)),
        )


def api_key_identity(key: str) -> str:
    return "key:" + hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def account_initials(name: str) -> str:
    """AccountInitials.tag(for:): 1 word -> first letter; >=2 words -> first + last word first letter."""
    words = [w for w in name.strip().split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()


# ---------------------------------------------------------------------------
# Priority thresholds
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Metrics, Reset Items, Usage History & Provider Snapshots
# ---------------------------------------------------------------------------

@dataclass
class MetricValue:
    number: float
    kind: str  # "percent", "dollars", "count"
    label: Optional[str] = None
    currency_code: Optional[str] = None  # e.g. "USD", "EUR"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "label": self.label,
            "currencyCode": self.currency_code,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetricValue":
        return cls(
            number=float(data.get("number", 0.0)),
            kind=str(data.get("kind", "count")),
            label=data.get("label"),
            currency_code=data.get("currencyCode"),
        )


@dataclass
class ResetItem:
    id: str
    provider_kind: str  # "claude", "codex", "zai"
    kind: str           # "session", "weekly", "fable", "full"
    title: str          # "Session Reset", "Weekly Reset", "Fable Reset", "Full Reset"
    expires_at: Optional[str] = None
    usable_now: bool = True
    claim_payload: Dict[str, Any] = field(default_factory=dict)
    unavailable_reason: Optional[str] = None

    KIND_ORDER = {"session": 0, "weekly": 1, "fable": 2, "full": 3}

    def sort_key(self) -> Tuple[str, int, str]:
        return (self.expires_at or "9999-12-31T23:59:59.999Z", self.KIND_ORDER.get(self.kind, 9), self.id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "providerKind": self.provider_kind,
            "kind": self.kind,
            "title": self.title,
            "expiresAt": self.expires_at,
            "usableNow": self.usable_now,
            "claimPayload": self.claim_payload,
            "unavailableReason": self.unavailable_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResetItem":
        return cls(
            id=str(data.get("id", "")),
            provider_kind=str(data.get("providerKind", "")),
            kind=str(data.get("kind", "full")),
            title=str(data.get("title", "Full Reset")),
            expires_at=data.get("expiresAt"),
            usable_now=bool(data.get("usableNow", True)),
            claim_payload=dict(data.get("claimPayload") or {}),
            unavailable_reason=data.get("unavailableReason"),
        )


@dataclass
class UsageBreakdownModel:
    name: str
    spend: Optional[float] = None
    tokens: Optional[float] = None
    share: Optional[float] = None
    is_other: bool = False  # the synthetic "Other" bucket folding every model past the top N

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "spend": self.spend,
            "tokens": self.tokens,
            "share": self.share,
            "isOther": self.is_other,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UsageBreakdownModel":
        return cls(
            name=str(data.get("name", "")),
            spend=data.get("spend"),
            tokens=data.get("tokens"),
            share=data.get("share"),
            is_other=bool(data.get("isOther")),
        )


@dataclass
class UsageBreakdown:
    title: str
    spend: Optional[float] = None
    currency_code: str = "USD"
    tokens: Optional[float] = None
    token_parts: Dict[str, float] = field(default_factory=dict)  # Input, Cache Read, Cache Write, Output, Reasoning
    models: List[UsageBreakdownModel] = field(default_factory=list)
    note: Optional[str] = None

    def top_models(self, limit: int = 5) -> List[UsageBreakdownModel]:
        if len(self.models) <= limit:
            return list(self.models)
        def rank(m: UsageBreakdownModel) -> Tuple[float, float, float]:
            return (m.spend or 0.0, m.tokens or 0.0, m.share or 0.0)
        ordered = sorted(self.models, key=rank, reverse=True)
        head = ordered[:limit]
        tail = ordered[limit:]
        other_spend = sum(m.spend for m in tail if m.spend is not None) if any(m.spend is not None for m in tail) else None
        other_tokens = sum(m.tokens for m in tail if m.tokens is not None) if any(m.tokens is not None for m in tail) else None
        other_share = sum(m.share for m in tail if m.share is not None) if any(m.share is not None for m in tail) else None
        head.append(UsageBreakdownModel(name="Other", spend=other_spend, tokens=other_tokens, share=other_share, is_other=True))
        return head

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "spend": self.spend,
            "currencyCode": self.currency_code,
            "tokens": self.tokens,
            "tokenParts": dict(self.token_parts),
            "models": [m.to_dict() for m in self.top_models()],
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UsageBreakdown":
        return cls(
            title=str(data.get("title", "")),
            spend=data.get("spend"),
            currency_code=str(data.get("currencyCode") or "USD"),
            tokens=data.get("tokens"),
            token_parts=dict(data.get("tokenParts") or {}),
            models=[UsageBreakdownModel.from_dict(m) for m in (data.get("models") or [])],
            note=data.get("note"),
        )


@dataclass
class MetricLine:
    """Single metric emitted by a provider runtime."""
    key: str
    title: str
    line_type: str  # "progress", "values", "badge"
    kind: str = "percent"  # "percent", "dollars", "count"
    used: float = 0.0
    limit: Optional[float] = None
    currency_code: Optional[str] = None
    unit_suffix: Optional[str] = None
    resets_at: Optional[str] = None  # ISO-8601 UTC
    period_duration_ms: Optional[int] = None
    session_start_signal: Optional[str] = None  # "zero_usage" | "missing_reset_date" | None
    values: List[MetricValue] = field(default_factory=list)
    badge_text: Optional[str] = None
    subtitle: Optional[str] = None
    expiries_at: List[str] = field(default_factory=list)
    reset_items: List[ResetItem] = field(default_factory=list)
    breakdown: Optional[UsageBreakdown] = None
    # Export metadata for omameter.limits.v2
    resource_key: Optional[str] = None
    resource_kind: str = "consumption"  # "consumption" | "balance"
    resource_unit: Optional[str] = None

    @property
    def is_bounded(self) -> bool:
        return self.line_type == "progress" and self.limit is not None and self.limit > 0

    @property
    def remaining(self) -> float:
        if self.limit is None:
            return 0.0
        return max(0.0, self.limit - self.used)

    @property
    def remaining_fraction(self) -> float:
        if not self.is_bounded or not self.limit:
            return 1.0
        return max(0.0, min(1.0, (self.limit - self.used) / self.limit))

    @property
    def used_fraction(self) -> float:
        if not self.is_bounded or not self.limit:
            return 0.0
        return max(0.0, min(1.0, self.used / self.limit))

    def rounded_at_precision(self, val: float) -> float:
        if self.kind == "percent":
            return round(val)
        if self.kind == "count":
            return round(val * 10.0) / 10.0
        if self.kind == "dollars":
            return round(val * 100.0) / 100.0
        return val

    @property
    def is_spent(self) -> bool:
        if not self.is_bounded or self.limit is None:
            return False
        return self.rounded_at_precision(self.limit - self.used) <= 0

    def severity(self, limits: PriorityLimits) -> str:
        if not self.is_bounded:
            return "normal"
        pct_left = int(round(self.remaining_fraction * 100.0))
        if pct_left <= limits.critical:
            return "critical"
        if pct_left <= limits.warning:
            return "warning"
        return "normal"

    def expiry_severity(self, now: datetime) -> str:
        if not self.expiries_at:
            return "normal"
        parsed = [parse_iso8601(x) for x in self.expiries_at]
        valid = [dt for dt in parsed if dt is not None]
        if not valid:
            return "normal"
        soonest = min(valid)
        delta = (soonest - now).total_seconds()
        if delta <= 48 * 3600:
            return "critical"
        if delta <= 7 * 24 * 3600:
            return "warning"
        return "normal"

    def is_empty_open_balance(self) -> bool:
        """Open balance (limit == None, not Limit Resets) at exactly 0."""
        if self.limit is not None or self.reset_items or self.expiries_at:
            return False
        if self.resource_kind != "balance":
            return False
        if not self.values:
            return False
        return self.values[0].number == 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "lineType": self.line_type,
            "kind": self.kind,
            "used": self.used,
            "limit": self.limit,
            "currencyCode": self.currency_code,
            "unitSuffix": self.unit_suffix,
            "resetsAt": self.resets_at,
            "periodDurationMs": self.period_duration_ms,
            "sessionStartSignal": self.session_start_signal,
            "values": [v.to_dict() for v in self.values],
            "badgeText": self.badge_text,
            "subtitle": self.subtitle,
            "expiriesAt": list(self.expiries_at),
            "resetItems": [r.to_dict() for r in self.reset_items],
            "breakdown": self.breakdown.to_dict() if self.breakdown else None,
            "resourceKey": self.resource_key,
            "resourceKind": self.resource_kind,
            "resourceUnit": self.resource_unit,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetricLine":
        return cls(
            key=str(data["key"]),
            title=str(data.get("title") or data["key"]),
            line_type=str(data.get("lineType") or "progress"),
            kind=str(data.get("kind") or "percent"),
            used=float(data.get("used", 0.0)),
            limit=float(data["limit"]) if data.get("limit") is not None else None,
            currency_code=data.get("currencyCode"),
            unit_suffix=data.get("unitSuffix"),
            resets_at=data.get("resetsAt"),
            period_duration_ms=int(data["periodDurationMs"]) if data.get("periodDurationMs") is not None else None,
            session_start_signal=data.get("sessionStartSignal"),
            values=[MetricValue.from_dict(v) for v in (data.get("values") or [])],
            badge_text=data.get("badgeText"),
            subtitle=data.get("subtitle"),
            expiries_at=list(data.get("expiriesAt") or []),
            reset_items=[ResetItem.from_dict(r) for r in (data.get("resetItems") or [])],
            breakdown=UsageBreakdown.from_dict(data["breakdown"]) if data.get("breakdown") else None,
            resource_key=data.get("resourceKey"),
            resource_kind=str(data.get("resourceKind") or "consumption"),
            resource_unit=data.get("resourceUnit"),
        )


@dataclass
class ProviderSnapshot:
    account_id: str
    card_id: str
    kind: str
    plan: Optional[str] = None
    lines: List[MetricLine] = field(default_factory=list)
    refreshed_at: str = field(default_factory=lambda: format_iso8601(utc_now()) or "")
    stale_at: str = field(default_factory=lambda: format_iso8601(utc_now() + timedelta(seconds=600)) or "")
    notice: Optional[str] = None  # Provider-reported informational notice (e.g. no quota on plan)
    account_label_discovered: Optional[str] = None  # Email/handle discovered from API

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        now = now or utc_now()
        dt = parse_iso8601(self.stale_at)
        if dt is None:
            return True
        return now >= dt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accountId": self.account_id,
            "cardId": self.card_id,
            "kind": self.kind,
            "plan": self.plan,
            "lines": [l.to_dict() for l in self.lines],
            "refreshedAt": self.refreshed_at,
            "staleAt": self.stale_at,
            "notice": self.notice,
            "accountLabelDiscovered": self.account_label_discovered,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderSnapshot":
        return cls(
            account_id=str(data["accountId"]),
            card_id=str(data["cardId"]),
            kind=str(data["kind"]),
            plan=data.get("plan"),
            lines=[MetricLine.from_dict(l) for l in (data.get("lines") or [])],
            refreshed_at=str(data.get("refreshedAt") or ""),
            stale_at=str(data.get("staleAt") or ""),
            notice=data.get("notice"),
            account_label_discovered=data.get("accountLabelDiscovered"),
        )


# ---------------------------------------------------------------------------
# Formatting helpers (MetricFormatter & Formatters)
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS: Dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "BRL": "R$",
    "CAD": "CA$",
    "AUD": "A$",
    "CHF": "CHF ",
    "INR": "₹",
}


def currency_symbol(code: Optional[str]) -> str:
    if not code:
        return "$"
    return CURRENCY_SYMBOLS.get(code.upper(), f"{code.upper()} ")


def format_compact_number(val: float) -> str:
    av = abs(val)
    sign = "-" if val < 0 else ""
    if av >= 1_000_000_000:
        n = av / 1_000_000_000.0
        s = f"{n:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{s}B"
    if av >= 1_000_000:
        n = av / 1_000_000.0
        s = f"{n:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{s}M"
    if av >= 1_000:
        n = av / 1_000.0
        s = f"{n:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{s}K"
    s = f"{av:.1f}".rstrip("0").rstrip(".")
    return f"{sign}{s}"


def format_currency(val: float, code: Optional[str] = "USD", *, style: str = "row") -> str:
    sym = currency_symbol(code)
    av = abs(val)
    sign = "-" if val < 0 else ""
    if av >= 1000:
        return f"{sign}{sym}{format_compact_number(av)}"
    if style == "tray":
        # Keep cents visible in the top bar: $11.30, $123.4 (a balance of $11
        # and $11.95 are not the same thing).
        if round(av, 2) < 100:
            return f"{sign}{sym}{av:.2f}"
        if round(av, 1) < 1000:
            return f"{sign}{sym}{av:.1f}"
        return f"{sign}{sym}{format_compact_number(1000.0)}"
    cents = int(round(av * 100))
    if cents % 100 == 0:
        return f"{sign}{sym}{cents // 100}"
    return f"{sign}{sym}{av:.2f}"


def format_metric_value(val: float, kind: str, *, currency_code: Optional[str] = None, style: str = "row", unit_suffix: Optional[str] = None) -> str:
    if kind == "percent":
        pct = max(0, min(100, int(round(val))))
        return f"{pct}%"
    if kind == "dollars":
        return format_currency(val, currency_code, style=style)
    num = format_compact_number(val)
    if unit_suffix and style == "row":
        return f"{num} {unit_suffix}"
    return num


def format_duration_seconds(seconds: float) -> str:
    """Rounds up to whole minutes; largest unit + next non-zero unit from [y, mo, d, h, m]."""
    if seconds <= 0:
        return "0m"
    total_minutes = int(math.ceil(seconds / 60.0))
    if total_minutes <= 0:
        return "1m"

    units = [
        ("y", 365 * 24 * 60),
        ("mo", 30 * 24 * 60),
        ("d", 24 * 60),
        ("h", 60),
        ("m", 1),
    ]
    parts: List[str] = []
    rem = total_minutes
    for idx, (suffix, size) in enumerate(units):
        if rem >= size:
            qty = rem // size
            rem %= size
            parts.append(f"{qty}{suffix}")
            # Include next unit down only if non-zero
            if idx + 1 < len(units):
                next_suffix, next_size = units[idx + 1]
                next_qty = rem // next_size
                if next_qty > 0:
                    parts.append(f"{next_qty}{next_suffix}")
            break
    return " ".join(parts) if parts else "1m"


def short_metric_label(title: str) -> str:
    """MenuBarContentBuilder.shortLabel: strips trailing generic words (models/bot/usage)."""
    words = title.strip().split()
    if len(words) > 1 and words[-1].lower() in {"models", "bot", "usage"}:
        return " ".join(words[:-1])
    return title.strip()


def title_names_window(short_title: str, window_tag: str) -> bool:
    """True when the (untranslated) short title already names its window, so the
    metric picker must not append the window tag ("Weekly" vs "Gemini" + "7d")."""
    base = (short_title or "").strip().lower()
    tag = (window_tag or "").strip().lower()
    if not base or not tag or tag in base:
        return True
    words = {
        "7d": ("week",),
        "5h": ("session",),
        "1d": ("day", "daily", "today"),
        "30d": ("month",),
        "$": ("extra", "credit", "balance", "spend"),
    }.get(tag, ())
    return any(w in base for w in words)


def metric_window_info(line: MetricLine) -> Tuple[str, str]:
    """Return (window_kind, window_tag) distinguishing 5h, session, daily, weekly, monthly, and extra limits."""
    key_l = (line.key or "").strip().lower()
    title_l = (line.title or "").strip().lower()
    res_l = (line.resource_key or "").strip().lower()
    pms = line.period_duration_ms

    def _period_tag() -> str:
        if pms is None or pms <= 0:
            return ""
        hours = int(round(pms / 3_600_000.0))
        if hours <= 0:
            return ""
        if hours < 24:
            return f"{hours}h"
        if hours == 24:
            return "1d"
        if hours == 168:
            return "7d"
        if 28 * 24 <= hours <= 31 * 24:
            return "30d"
        if hours % 24 == 0:
            return f"{hours // 24}d"
        return f"{hours}h"

    if "5h" in title_l or key_l.endswith("5h"):
        return "5h", "5h"
    if "session" in key_l or "session" in title_l or "session" in res_l:
        return "session", _period_tag() or "5h"
    if "week" in key_l or "week" in title_l or "week" in res_l or "7d" in title_l:
        return "weekly", "7d"
    if "daily" in key_l or "daily" in title_l or "today" in title_l or "24h" in title_l or "1d" in title_l:
        return "daily", "1d"
    if "month" in key_l or "month" in title_l or "30d" in title_l:
        return "monthly", "30d"

    if pms is not None and pms > 0:
        hours = int(round(pms / 3_600_000.0))
        if hours == 5:
            return "5h", "5h"
        if 0 < hours < 24:
            return "session", f"{hours}h"
        if hours == 24:
            return "daily", "1d"
        if hours == 168:
            return "weekly", "7d"
        if 28 * 24 <= hours <= 31 * 24:
            return "monthly", "30d"
        if hours > 24 and hours % 24 == 0:
            days = hours // 24
            return ("weekly" if days <= 14 else "monthly"), f"{days}d"
        if hours > 0:
            return "session", f"{hours}h"

    is_reset_credits = (
        bool(line.reset_items)
        or "reset" in key_l
        or (line.resource_unit or "").strip().lower() == "resets"
    )
    if not is_reset_credits and (
        line.kind == "dollars"
        or line.resource_kind == "balance"
        or "extra" in key_l
        or "credit" in key_l
        or "balance" in key_l
    ):
        return "extra", "$"

    return "", ""

