"""Versioned, official-source model pricing and deterministic cost estimates.

Prices are never accepted from browser input.  A run freezes the matched rule
so later catalogue updates cannot rewrite historical estimates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit


CATALOG_VERSION = "2026-09-21.1"
VERIFIED_AT = "2026-09-21"

OFFICIAL_HOSTS = {
    "deepseek": {"api.deepseek.com"},
    "openai": {"api.openai.com"},
    "anthropic": {"api.anthropic.com"},
    "gemini": {"generativelanguage.googleapis.com"},
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _rule(rule_id, provider, models, rates, source_url, *, currency="USD", effective_at="2026-09-01"):
    return {
        "rule_id": rule_id,
        "provider": provider,
        "models": models,
        "currency": currency,
        "rates": rates,
        "source_url": source_url,
        "effective_at": effective_at,
    }


# Per million tokens.  Entries deliberately use exact official model IDs and
# documented aliases; unknown IDs remain unpriced instead of inheriting a
# similar model's rate.
RULES = [
    _rule(
        "deepseek-flash-2026-09", "deepseek",
        ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"],
        {
            "off_peak": {"input": 0.15, "cache_read": 0.003, "output": 0.60},
            "peak": {"input": 0.30, "cache_read": 0.006, "output": 1.20},
        },
        "https://api-docs.deepseek.com/quick_start/pricing/",
    ),
    _rule(
        "deepseek-v4-pro-2026-09", "deepseek", ["deepseek-v4-pro", "deepseek-v4-pro-0813"],
        {
            "off_peak": {"input": 0.66, "cache_read": 0.022, "output": 1.98},
            "peak": {"input": 1.32, "cache_read": 0.044, "output": 3.96},
        },
        "https://api-docs.deepseek.com/quick_start/pricing/",
    ),
    _rule(
        "claude-sonnet-5-2026-09", "anthropic", ["claude-sonnet-5"],
        {"standard": {"input": 2.00, "cache_read": 0.20, "cache_write_5m": 2.50,
                      "cache_write_1h": 4.00, "output": 10.00}},
        "https://platform.claude.com/docs/en/about-claude/pricing",
    ),
    _rule(
        "claude-sonnet-4x", "anthropic", ["claude-sonnet-4-6", "claude-sonnet-4-5"],
        {"standard": {"input": 3.00, "cache_read": 0.30, "cache_write_5m": 3.75,
                      "cache_write_1h": 6.00, "output": 15.00}},
        "https://platform.claude.com/docs/en/about-claude/pricing",
    ),
    _rule(
        "claude-haiku-4-5", "anthropic", ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
        {"standard": {"input": 1.00, "cache_read": 0.10, "cache_write_5m": 1.25,
                      "cache_write_1h": 2.00, "output": 5.00}},
        "https://platform.claude.com/docs/en/about-claude/pricing",
    ),
    _rule(
        "openai-gpt-6-astra", "openai", ["gpt-6-astra"],
        {"standard": {"input": 10.00, "cache_read": 1.00, "cache_write": 12.50, "output": 50.00}},
        "https://developers.openai.com/api/docs/models/compare",
    ),
    _rule(
        "openai-gpt-5-6-sol", "openai", ["gpt-5.6-sol"],
        {"standard": {"input": 4.00, "cache_read": 0.40, "output": 20.00}},
        "https://developers.openai.com/api/docs/models/compare",
    ),
    _rule(
        "openai-gpt-5-6-terra", "openai", ["gpt-5.6-terra"],
        {"standard": {"input": 2.00, "cache_read": 0.20, "output": 12.00}},
        "https://developers.openai.com/api/docs/models/compare",
    ),
    _rule(
        "gemini-3-5-flash", "gemini", ["gemini-3.5-flash"],
        {"standard": {"input": 1.50, "cache_read": 0.15, "output": 9.00}},
        "https://ai.google.dev/gemini-api/docs/pricing",
    ),
]


def _value(profile, key, default=""):
    return profile.get(key, default) if isinstance(profile, dict) else getattr(profile, key, default)


def _model_id(value):
    model = str(value or "").strip().lower()
    for prefix in ("openai/", "anthropic/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
    return model


def _stamp(value=None):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _deepseek_tier(moment):
    # Official peak hours: 01:00-04:00 and 06:00-10:00 UTC, Monday-Friday.
    hour = moment.hour
    return "peak" if moment.weekday() < 5 and (1 <= hour < 4 or 6 <= hour < 10) else "off_peak"


def resolve_pricing(profile, effective_at=None):
    """Return a frozen pricing snapshot or an explicit unavailable reason."""
    provider = str(_value(profile, "provider") or "custom").lower()
    model = _model_id(_value(profile, "model"))
    base_url = str(_value(profile, "base_url"))
    host = (urlsplit(base_url).hostname or "").lower()
    common = {
        "schema_version": "pricing-v1",
        "catalog_version": CATALOG_VERSION,
        "provider": provider,
        "model": model,
        "verified_at": VERIFIED_AT,
        "resolved_at": _stamp(effective_at).isoformat(timespec="seconds"),
    }
    if provider == "ollama" and host in LOCAL_HOSTS:
        return {**common, "status": "local", "currency": "CNY", "rates": {"input": 0.0, "output": 0.0},
                "rule_id": "ollama-local", "rate_tier": "local",
                "source_url": "https://ollama.com/", "effective_at": VERIFIED_AT}
    if provider not in OFFICIAL_HOSTS or host not in OFFICIAL_HOSTS[provider]:
        return {**common, "status": "unavailable", "reason": "non_official_endpoint"}
    rule = next((item for item in RULES if item["provider"] == provider and model in item["models"]), None)
    if not rule:
        return {**common, "status": "unavailable", "reason": "model_not_in_catalog"}
    moment = _stamp(effective_at)
    tier = _deepseek_tier(moment) if provider == "deepseek" else "standard"
    rates = rule["rates"][tier]
    return {
        **common, "status": "official", "rule_id": rule["rule_id"], "currency": rule["currency"],
        "rate_tier": tier, "rates": rates, "source_url": rule["source_url"],
        "effective_at": rule["effective_at"],
    }


def _legacy_pricing(pricing):
    if not pricing or pricing.get("rates"):
        return pricing
    input_rate = pricing.get("input_price_per_million")
    output_rate = pricing.get("output_price_per_million")
    if input_rate is None or output_rate is None:
        return pricing
    return {
        **pricing, "schema_version": "pricing-legacy", "status": "legacy_manual",
        "rates": {"input": input_rate, "cache_read": pricing.get("cached_input_price_per_million", input_rate),
                  "output": output_rate},
    }


def cost_metrics(usage, pricing):
    """Calculate a transparent estimate from provider-reported usage only."""
    pricing = _legacy_pricing(pricing or {})
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = min(input_tokens, int(usage.get("cached_input_tokens") or 0))
    cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
    total_tokens = int(usage.get("usage_tokens") or input_tokens + output_tokens)
    base = {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens, "model_calls": int(usage.get("model_calls") or 0),
        "currency": pricing.get("currency", "USD"), "pricing": {
            key: pricing.get(key) for key in ("status", "catalog_version", "rule_id", "rate_tier",
                                               "source_url", "verified_at", "effective_at", "reason")
            if pricing.get(key) is not None
        },
    }
    if pricing.get("status") == "unavailable" or not pricing.get("rates"):
        return {**base, "cost": None, "cost_status": "unavailable", "cost_partial": True, "cost_breakdown": []}
    if total_tokens and not (input_tokens or output_tokens):
        return {**base, "cost": None, "cost_status": "partial", "cost_partial": True, "cost_breakdown": []}
    rates = pricing["rates"]
    categories = [
        ("input", max(0, input_tokens - cached_tokens - cache_write_tokens), rates.get("input")),
        ("cache_read", cached_tokens, rates.get("cache_read", rates.get("input"))),
        ("cache_write", cache_write_tokens, rates.get("cache_write") or rates.get("cache_write_5m")),
        ("output", output_tokens, rates.get("output")),
    ]
    incomplete = bool(usage.get("usage_unknown"))
    breakdown, cost = [], 0.0
    for category, tokens, rate in categories:
        if tokens and rate is None:
            incomplete = True
            continue
        amount = tokens * float(rate or 0) / 1_000_000
        if tokens:
            breakdown.append({"category": category, "tokens": tokens, "rate_per_million": rate, "amount": round(amount, 8)})
        cost += amount
    status = "local" if pricing.get("status") == "local" else ("partial" if incomplete else "estimated")
    return {**base, "cost": round(cost, 8), "cost_status": status,
            "cost_partial": incomplete, "cost_breakdown": breakdown}
