"""Model pricing engine: bundled catalog, on-demand LiteLLM sync, custom overrides, and aliases.

Rates are expressed in USD per 1,000,000 tokens. Supports:
- Input, Output, Cache Read, Cache Write (5m), and Cache Write (1h) rates
- Context-length tiers (e.g., >100K / >200K / >272K prompt tokens)
- Speed tiers (standard, fast / priority, ultrafast)
- User custom model prices and "Map to" model aliases
- Explicit unpriced tracking (never fabricates prices for unknown models)
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.http_client import HTTPClient, ProxyConfig
from backend.store import CACHE_DIR, CONFIG_DIR, _atomic_write_json, _read_json

LITELLM_RATES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
BUNDLED_CATALOG_PATH = Path(__file__).resolve().parent / "pricing_catalog.json"
LITELLM_CACHE_PATH = CACHE_DIR / "litellm_rates.json"
ANALYTICS_CONFIG_PATH = CONFIG_DIR / "analytics.json"

FIRST_PARTY_PROVIDERS = frozenset(
    {"anthropic", "deepseek", "gemini", "google", "meta", "mistral", "openai", "xai"}
)
CONTEXT_TIER_RE = re.compile(
    r"^input_cost_per_token_above_(\d{1,7})k_tokens(?:_priority|_ultrafast)?$"
)
DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
ISO_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")

DEFAULT_ANALYTICS_CONFIG: Dict[str, Any] = {
    "enabledSources": {
        "claude": True,
        "codex": True,
        "antigravity": True,
        "opencode": True,
    },
    "customPaths": {
        "claude": [],
        "codex": [],
        "antigravity": [],
        "opencode": [],
    },
    "priceOverrides": {},
    "modelAliases": {},
    "planMonthlyPrices": {},
    "retentionDays": 92,
}


def _finite(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) and f >= 0.0 else None


@dataclass(frozen=True)
class TokenRates:
    """USD per 1M tokens."""
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float
    cache_write_per_m: float
    cache_write_1h_per_m: float

    def scaled(self, factor: float) -> "TokenRates":
        return TokenRates(
            input_per_m=self.input_per_m * factor,
            output_per_m=self.output_per_m * factor,
            cache_read_per_m=self.cache_read_per_m * factor,
            cache_write_per_m=self.cache_write_per_m * factor,
            cache_write_1h_per_m=self.cache_write_1h_per_m * factor,
        )


@dataclass(frozen=True)
class ContextTier:
    threshold_tokens: int
    standard: TokenRates
    fast: Optional[TokenRates] = None
    ultrafast: Optional[TokenRates] = None


@dataclass(frozen=True)
class ModelRate:
    standard: TokenRates
    fast: Optional[TokenRates] = None
    ultrafast: Optional[TokenRates] = None
    context_tiers: Tuple[ContextTier, ...] = ()
    internal: bool = False
    source: str = "bundled"


@dataclass(frozen=True)
class PriceEvaluation:
    cost_usd: Optional[float]
    cache_savings_usd: float
    priced: bool
    cost_source: str  # "override" | "catalog" | "reported" | "internal" | "unpriced"
    resolved_model: str


def normalize_model_key(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s.startswith("~"):
        s = s[1:].strip()
    return s


def bare_model_name(key: str) -> str:
    if "/" in key:
        return key.rsplit("/", 1)[-1].strip()
    return key


def strip_date_suffix(key: str) -> str:
    out = ISO_DATE_SUFFIX_RE.sub("", key)
    out = DATE_SUFFIX_RE.sub("", out)
    return out


# ---------------------------------------------------------------------------
# Analytics configuration persistence (~/.config/omameter/analytics.json)
# ---------------------------------------------------------------------------

def normalize_analytics_config(raw: Any) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    out = copy.deepcopy(DEFAULT_ANALYTICS_CONFIG)

    enabled = src.get("enabledSources")
    if isinstance(enabled, dict):
        for k in out["enabledSources"]:
            if isinstance(enabled.get(k), bool):
                out["enabledSources"][k] = enabled[k]

    paths = src.get("customPaths")
    if isinstance(paths, dict):
        for k in out["customPaths"]:
            vals = paths.get(k)
            if isinstance(vals, list):
                cleaned: List[str] = []
                for item in vals:
                    if isinstance(item, str) and item.strip():
                        p = item.strip()
                        if p not in cleaned:
                            cleaned.append(p)
                out["customPaths"][k] = cleaned

    overrides = src.get("priceOverrides")
    if isinstance(overrides, dict):
        clean_ov: Dict[str, Dict[str, float]] = {}
        for mid, entry in overrides.items():
            if not isinstance(mid, str) or not mid.strip() or not isinstance(entry, dict):
                continue
            inp = _finite(entry.get("inputPerM"))
            outp = _finite(entry.get("outputPerM"))
            if inp is None or outp is None:
                continue
            cr = _finite(entry.get("cacheReadPerM"))
            cw = _finite(entry.get("cacheWritePerM"))
            cw1h = _finite(entry.get("cacheWrite1hPerM"))
            item_ov: Dict[str, float] = {
                "inputPerM": round(inp, 6),
                "outputPerM": round(outp, 6),
            }
            if cr is not None:
                item_ov["cacheReadPerM"] = round(cr, 6)
            if cw is not None:
                item_ov["cacheWritePerM"] = round(cw, 6)
            if cw1h is not None:
                item_ov["cacheWrite1hPerM"] = round(cw1h, 6)
            clean_ov[mid.strip()] = item_ov
        out["priceOverrides"] = clean_ov

    aliases = src.get("modelAliases")
    if isinstance(aliases, dict):
        clean_al: Dict[str, str] = {}
        for src_m, dst_m in aliases.items():
            if isinstance(src_m, str) and isinstance(dst_m, str):
                s, d = src_m.strip(), dst_m.strip()
                if s and d and s != d:
                    clean_al[s] = d
        out["modelAliases"] = clean_al

    plans = src.get("planMonthlyPrices")
    if isinstance(plans, dict):
        clean_plans: Dict[str, float] = {}
        for prov, price in plans.items():
            if not isinstance(prov, str) or not prov.strip():
                continue
            val = _finite(price)
            if val is not None and val > 0:
                clean_plans[prov.strip().lower()] = round(val, 2)
        out["planMonthlyPrices"] = clean_plans

    ret = src.get("retentionDays")
    if isinstance(ret, int) and not isinstance(ret, bool) and ret in (0, 30, 90, 92, 180, 365):
        out["retentionDays"] = ret

    return out


def load_analytics_config(path: Path = ANALYTICS_CONFIG_PATH) -> Dict[str, Any]:
    return normalize_analytics_config(_read_json(path, DEFAULT_ANALYTICS_CONFIG))


def save_analytics_config(config: Dict[str, Any], path: Path = ANALYTICS_CONFIG_PATH) -> Dict[str, Any]:
    normalized = normalize_analytics_config(config)
    _atomic_write_json(path, normalized, mode=0o600)
    return normalized


# ---------------------------------------------------------------------------
# Catalog loaders (bundled + LiteLLM online snapshot)
# ---------------------------------------------------------------------------

def _bundled_entry_to_rate(entry: Mapping[str, Any]) -> Optional[ModelRate]:
    inp = _finite(entry.get("input"))
    outp = _finite(entry.get("output"))
    if inp is None or outp is None:
        return None
    cr = _finite(entry.get("cacheRead"))
    cw = _finite(entry.get("cacheWrite"))
    cw1h = _finite(entry.get("cacheWrite1h"))
    base = TokenRates(
        input_per_m=inp,
        output_per_m=outp,
        cache_read_per_m=cr if cr is not None else inp,
        cache_write_per_m=cw if cw is not None else inp,
        cache_write_1h_per_m=cw1h if cw1h is not None else (cw if cw is not None else inp),
    )
    fast_mult = _finite(entry.get("fastMultiplier"))
    ultra_mult = _finite(entry.get("ultrafastMultiplier"))
    fast = base.scaled(fast_mult) if fast_mult and fast_mult > 0 else None
    ultra = base.scaled(ultra_mult) if ultra_mult and ultra_mult > 0 else None

    tiers: List[ContextTier] = []
    raw_tiers = entry.get("contextTiers")
    if isinstance(raw_tiers, list):
        for t in raw_tiers:
            if not isinstance(t, dict):
                continue
            thr = t.get("threshold")
            ti = _finite(t.get("input"))
            to = _finite(t.get("output"))
            if not isinstance(thr, int) or thr <= 0 or ti is None or to is None:
                continue
            tcr = _finite(t.get("cacheRead"))
            tcw = _finite(t.get("cacheWrite"))
            tcw1h = _finite(t.get("cacheWrite1h"))
            trates = TokenRates(
                input_per_m=ti,
                output_per_m=to,
                cache_read_per_m=tcr if tcr is not None else ti,
                cache_write_per_m=tcw if tcw is not None else ti,
                cache_write_1h_per_m=tcw1h if tcw1h is not None else (tcw if tcw is not None else ti),
            )
            tiers.append(
                ContextTier(
                    threshold_tokens=thr,
                    standard=trates,
                    fast=trates.scaled(fast_mult) if fast_mult and fast_mult > 0 else None,
                    ultrafast=trates.scaled(ultra_mult) if ultra_mult and ultra_mult > 0 else None,
                )
            )
    tiers.sort(key=lambda x: x.threshold_tokens, reverse=True)
    return ModelRate(
        standard=base,
        fast=fast,
        ultrafast=ultra,
        context_tiers=tuple(tiers),
        internal=bool(entry.get("internal")),
        source="bundled",
    )


def _litellm_token_rates(
    entry: Mapping[str, Any],
    suffix: str,
    standard: Optional[TokenRates] = None,
    output_fallback: Optional[float] = None,
) -> Optional[TokenRates]:
    raw_in = _finite(entry.get(f"input_cost_per_token{suffix}"))
    raw_out = _finite(entry.get(f"output_cost_per_token{suffix}"))
    if raw_in is None:
        return None
    inp = raw_in * 1_000_000.0
    if raw_out is not None:
        outp = raw_out * 1_000_000.0
    elif output_fallback is not None:
        outp = output_fallback
    else:
        return None

    def _cache_rate(field_name: str, std_attr: str, fallback: float) -> float:
        v = _finite(entry.get(f"{field_name}{suffix}"))
        if v is not None:
            return v * 1_000_000.0
        if standard is not None and standard.input_per_m > 0:
            return (getattr(standard, std_attr) / standard.input_per_m) * inp
        return fallback

    cw = _cache_rate("cache_creation_input_token_cost", "cache_write_per_m", inp)
    cr = _cache_rate("cache_read_input_token_cost", "cache_read_per_m", inp)
    cw1h = _cache_rate("cache_creation_input_token_cost_above_1hr", "cache_write_1h_per_m", cw)
    return TokenRates(
        input_per_m=inp,
        output_per_m=outp,
        cache_read_per_m=cr,
        cache_write_per_m=cw,
        cache_write_1h_per_m=cw1h,
    )


def parse_litellm_table(document: Any) -> Dict[str, ModelRate]:
    """Parse a LiteLLM model_prices_and_context_window.json object into ModelRates."""
    if not isinstance(document, dict):
        return {}
    table: Dict[str, ModelRate] = {}
    for raw_name, raw_entry in document.items():
        if not isinstance(raw_name, str) or not isinstance(raw_entry, dict):
            continue
        std = _litellm_token_rates(raw_entry, "")
        if std is None:
            continue
        key = normalize_model_key(raw_name)
        if not key:
            continue
        specific = raw_entry.get("provider_specific_entry")
        fast_mult = _finite(specific.get("fast")) if isinstance(specific, dict) else None
        fast = (
            std.scaled(fast_mult)
            if fast_mult and fast_mult > 0
            else _litellm_token_rates(raw_entry, "_priority", std)
        )
        ultra = _litellm_token_rates(raw_entry, "_ultrafast", std)

        thresholds = set()
        for k in raw_entry:
            m = CONTEXT_TIER_RE.match(k)
            if m:
                thresholds.add(int(m.group(1)))
        tiers: List[ContextTier] = []
        for thr_k in sorted(thresholds, reverse=True):
            sfx = f"_above_{thr_k}k_tokens"
            t_std = _litellm_token_rates(raw_entry, sfx, std, std.output_per_m) or std
            t_fast = (
                t_std.scaled(fast_mult)
                if fast_mult and fast_mult > 0
                else _litellm_token_rates(raw_entry, f"{sfx}_priority", t_std)
            )
            t_ultra = _litellm_token_rates(raw_entry, f"{sfx}_ultrafast", t_std)
            tiers.append(
                ContextTier(
                    threshold_tokens=thr_k * 1_000,
                    standard=t_std,
                    fast=t_fast,
                    ultrafast=t_ultra,
                )
            )
        table[key] = ModelRate(
            standard=std,
            fast=fast,
            ultrafast=ultra,
            context_tiers=tuple(tiers),
            source="litellm",
        )

    # Promote bare names from first-party provider prefixes when not directly listed.
    bare_candidates: Dict[str, Tuple[ModelRate, bool]] = {}
    for key, rate in table.items():
        if "/" not in key:
            continue
        prefix, bare = key.split("/", 1)
        bare = bare.strip()
        if not bare or bare in table:
            continue
        first_party = prefix in FIRST_PARTY_PROVIDERS
        existing = bare_candidates.get(bare)
        if existing is None or (first_party and not existing[1]):
            bare_candidates[bare] = (rate, first_party)
    for bare, (rate, _) in bare_candidates.items():
        table[bare] = rate
    return table


class PricingEngine:
    """Resolves model aliases, rates, and USD cost / cache savings for token events."""

    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        bundled_path: Path = BUNDLED_CATALOG_PATH,
        litellm_cache_path: Path = LITELLM_CACHE_PATH,
    ) -> None:
        self.bundled_path = bundled_path
        self.litellm_cache_path = litellm_cache_path
        self.config = normalize_analytics_config(config)
        self.bundled_updated_at: str = ""
        self.litellm_fetched_at: int = 0
        self._bundled_rates: Dict[str, ModelRate] = {}
        self._litellm_rates: Dict[str, ModelRate] = {}
        self._override_rates: Dict[str, ModelRate] = {}
        self._aliases: Dict[str, str] = {}
        self._load_bundled()
        self._load_litellm_cache()
        self._rebuild_user_tables()

    def _load_bundled(self) -> None:
        raw = _read_json(self.bundled_path, {})
        if not isinstance(raw, dict):
            return
        self.bundled_updated_at = str(raw.get("updatedAt") or "")
        models = raw.get("models")
        if not isinstance(models, dict):
            return
        out: Dict[str, ModelRate] = {}
        for name, entry in models.items():
            if not isinstance(entry, dict):
                continue
            rate = _bundled_entry_to_rate(entry)
            if rate is not None:
                out[normalize_model_key(name)] = rate
        self._bundled_rates = out

    def _load_litellm_cache(self) -> None:
        raw = _read_json(self.litellm_cache_path, {})
        if not isinstance(raw, dict):
            return
        fetched = raw.get("fetchedAt")
        if isinstance(fetched, (int, float)) and math.isfinite(fetched):
            self.litellm_fetched_at = int(fetched)
        doc = raw.get("document")
        if isinstance(doc, dict):
            self._litellm_rates = parse_litellm_table(doc)

    def _rebuild_user_tables(self) -> None:
        ov: Dict[str, ModelRate] = {}
        for mid, entry in (self.config.get("priceOverrides") or {}).items():
            inp = float(entry["inputPerM"])
            outp = float(entry["outputPerM"])
            cr = float(entry.get("cacheReadPerM", inp))
            cw = float(entry.get("cacheWritePerM", inp))
            cw1h = float(entry.get("cacheWrite1hPerM", cw))
            ov[normalize_model_key(mid)] = ModelRate(
                standard=TokenRates(
                    input_per_m=inp,
                    output_per_m=outp,
                    cache_read_per_m=cr,
                    cache_write_per_m=cw,
                    cache_write_1h_per_m=cw1h,
                ),
                source="override",
            )
        self._override_rates = ov
        self._aliases = {
            normalize_model_key(k): v.strip()
            for k, v in (self.config.get("modelAliases") or {}).items()
            if k.strip() and v.strip()
        }

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = normalize_analytics_config(config)
        self._rebuild_user_tables()

    def refresh_litellm_online(self, proxy: Optional[ProxyConfig] = None) -> Tuple[bool, str, int]:
        """Download LiteLLM pricing snapshot on demand (respecting Omameter proxy)."""
        from backend.http_client import HTTPTransportError

        http = HTTPClient(proxy=proxy or ProxyConfig.load())
        try:
            # LiteLLM's model_prices_and_context_window.json is ~5 MiB and grows as new models launch.
            resp = http.get(LITELLM_RATES_URL, timeout=25.0, max_bytes=16 * 1024 * 1024)
        except HTTPTransportError as err:
            return False, str(err), 0
        if resp.status < 200 or resp.status >= 300:
            return False, f"HTTP {resp.status}", 0
        try:
            doc = json.loads(resp.body.decode("utf-8", errors="replace"))
        except ValueError as err:
            return False, str(err), 0
        parsed = parse_litellm_table(doc)
        if not parsed:
            return False, "Empty pricing catalog", 0
        # Preserve any previously cached entries that upstream removed.
        merged_doc: Dict[str, Any] = {}
        old = _read_json(self.litellm_cache_path, {})
        if isinstance(old, dict) and isinstance(old.get("document"), dict):
            merged_doc.update(old["document"])
        merged_doc.update(doc)
        now_s = int(time.time())
        _atomic_write_json(
            self.litellm_cache_path,
            {"fetchedAt": now_s, "source": LITELLM_RATES_URL, "document": merged_doc},
            mode=0o600,
        )
        self.litellm_fetched_at = now_s
        self._litellm_rates = parse_litellm_table(merged_doc)
        return True, "", len(self._litellm_rates)

    def resolve_alias(self, model: str) -> str:
        """Follow user 'Map to' aliases (bounded to prevent cycles)."""
        current = str(model or "unknown").strip() or "unknown"
        seen = set()
        for _ in range(5):
            norm = normalize_model_key(current)
            if norm in seen:
                break
            seen.add(norm)
            target = self._aliases.get(norm)
            if not target:
                break
            current = target
        return current

    def lookup_rate(self, model: str, route: str = "") -> Optional[ModelRate]:
        """Find the best matching ModelRate: user override -> bundled -> LiteLLM cache."""
        norm = normalize_model_key(model)
        if not norm or norm == "unknown":
            return None
        bare = bare_model_name(norm)
        route_norm = normalize_model_key(route)

        candidates = [norm]
        if route_norm and "/" not in norm:
            candidates.insert(0, f"{route_norm}/{norm}")
        if bare != norm:
            candidates.append(bare)
        stripped = strip_date_suffix(bare)
        if stripped and stripped not in candidates:
            candidates.append(stripped)
        # Normalize Anthropic dot vs dash minor versions (e.g. claude-opus-4.6 -> claude-opus-4-6)
        dash_variant = re.sub(r"(\d)\.(\d)", r"\1-\2", stripped)
        if dash_variant and dash_variant not in candidates:
            candidates.append(dash_variant)

        for table in (self._override_rates, self._bundled_rates, self._litellm_rates):
            for key in candidates:
                rate = table.get(key)
                if rate is not None:
                    return rate
        return None

    def evaluate(
        self,
        *,
        model: str,
        route: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cache_write_1h_tokens: int = 0,
        speed_tier: str = "standard",
        reported_cost_usd: Optional[float] = None,
    ) -> PriceEvaluation:
        resolved = self.resolve_alias(model)
        rate = self.lookup_rate(resolved, route=route)

        # User explicit override always wins.
        if rate is not None and rate.source == "override":
            cost, savings = self._compute(
                rate,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_write_1h_tokens=cache_write_1h_tokens,
                speed_tier=speed_tier,
            )
            return PriceEvaluation(cost, savings, True, "override", resolved)

        # For aggregator routes (like OpenRouter via OpenCode), a positive
        # harness-reported cost reflects actual billed cost.
        if (
            reported_cost_usd is not None
            and math.isfinite(reported_cost_usd)
            and reported_cost_usd > 0.0
            and normalize_model_key(route) in ("openrouter", "opencode", "zen")
        ):
            savings = 0.0
            if rate is not None:
                _, savings = self._compute(
                    rate,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    cache_write_1h_tokens=cache_write_1h_tokens,
                    speed_tier=speed_tier,
                )
            return PriceEvaluation(float(reported_cost_usd), savings, True, "reported", resolved)

        if rate is not None:
            if rate.internal:
                return PriceEvaluation(0.0, 0.0, True, "internal", resolved)
            cost, savings = self._compute(
                rate,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_write_1h_tokens=cache_write_1h_tokens,
                speed_tier=speed_tier,
            )
            return PriceEvaluation(cost, savings, True, "catalog", resolved)

        if reported_cost_usd is not None and math.isfinite(reported_cost_usd) and reported_cost_usd > 0.0:
            return PriceEvaluation(float(reported_cost_usd), 0.0, True, "reported", resolved)

        return PriceEvaluation(None, 0.0, False, "unpriced", resolved)

    @staticmethod
    def _compute(
        rate: ModelRate,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cache_write_1h_tokens: int,
        speed_tier: str,
    ) -> Tuple[float, float]:
        prompt_tokens = max(0, input_tokens) + max(0, cache_read_tokens) + max(0, cache_write_tokens)
        active_std = rate.standard
        active_fast = rate.fast
        active_ultra = rate.ultrafast

        for tier in rate.context_tiers:
            if prompt_tokens > tier.threshold_tokens:
                active_std = tier.standard
                active_fast = tier.fast or active_fast
                active_ultra = tier.ultrafast or active_ultra
                break

        selected = active_std
        if speed_tier == "ultrafast" and active_ultra is not None:
            selected = active_ultra
        elif speed_tier == "fast" and active_fast is not None:
            selected = active_fast

        cw_1h = max(0, min(cache_write_tokens, cache_write_1h_tokens))
        cw_5m = max(0, cache_write_tokens - cw_1h)

        cost = (
            max(0, input_tokens) * selected.input_per_m
            + max(0, output_tokens) * selected.output_per_m
            + max(0, cache_read_tokens) * selected.cache_read_per_m
            + cw_5m * selected.cache_write_per_m
            + cw_1h * selected.cache_write_1h_per_m
        ) / 1_000_000.0

        savings = (
            max(0, cache_read_tokens) * max(0.0, selected.input_per_m - selected.cache_read_per_m)
        ) / 1_000_000.0

        return cost, savings

    def catalog_info(self) -> Dict[str, Any]:
        return {
            "bundledUpdatedAt": self.bundled_updated_at,
            "bundledCount": len(self._bundled_rates),
            "litellmFetchedAt": self.litellm_fetched_at,
            "litellmCount": len(self._litellm_rates),
            "overrideCount": len(self._override_rates),
            "aliasCount": len(self._aliases),
        }
