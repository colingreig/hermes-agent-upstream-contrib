#!/usr/bin/env python3
"""validator_model.py — resolve the validator's model fallback CHAIN, future-proof.

The panel must "use the best model, and if it changes in the future use the new
best" + fall back Claude -> Gemini -> MiniMax (Colin, 2026-06-18). So we do NOT
hardcode a model id. Instead we declare a stable PREFERENCE of (vendor, family)
and resolve each to the highest-version concrete id available in Hermes's live
model catalog (`~/.hermes/cache/model_catalog.json`, refreshed by `hermes model
--refresh`). When a newer Opus / Gemini lands in the catalog, it is picked
automatically — no code change.

resolve_chain(tier) -> ordered list of {provider, model, label} to try in order.
The panel tries entry 1; on a provider/model error it falls through to entry 2,
etc. — giving the Claude -> Gemini -> MiniMax failover natively.

Override per tier with env (comma list of `provider:model`), e.g.
  VALIDATOR_HIGH_CHAIN="anthropic:claude-opus-4-8,google:gemini-3.1-pro-preview,minimax:MiniMax-M3"
An explicit override wins over catalog resolution (escape hatch / pinning).
"""
import json
import os
import re

CATALOG_PATH = os.path.expanduser("~/.hermes/cache/model_catalog.json")

# Stable preference. Each entry: (vendor, family). Resolved to the best live id.
# high  = Claude (best) -> Claude (next) -> Gemini pro -> MiniMax   (Colin's chain)
# medium= Claude haiku (deterministic, OAuth subscription) -> MiniMax fallback
#         (was Gemini flash primary; it flip-flopped PASS/BLOCK on the same diff)
PREFERENCES = {
    "high": [("anthropic", "opus"), ("anthropic", "sonnet"),
             ("google", "pro"), ("minimax", "m3")],
    "medium": [("anthropic", "haiku"), ("minimax", "m3")],
    "low": [],
}

# vendor -> hermes provider name (the `--provider` value `hermes -z` expects)
VENDOR_PROVIDER = {"anthropic": "anthropic", "google": "google", "minimax": "minimax"}

# Hardcoded last-resort defaults if the catalog is unreadable (kept current but
# only used when catalog resolution finds nothing).
SAFE_DEFAULTS = {
    ("anthropic", "opus"): "claude-opus-4-8",
    ("anthropic", "sonnet"): "claude-sonnet-5",
    ("anthropic", "haiku"): "claude-haiku-4-5-20251001",
    ("google", "pro"): "gemini-3.1-pro-preview",
    ("google", "flash"): "gemini-3.5-flash",
    ("minimax", "m3"): "MiniMax-M3",
}


def _version_key(model_id: str):
    """Extract a sortable version tuple from an id (e.g. 'claude-opus-4.8' -> (4,8))."""
    nums = re.findall(r"(\d+)(?:\.(\d+))?", model_id)
    if not nums:
        return (0, 0)
    # take the FIRST version-looking token (skip trailing date stamps)
    major, minor = nums[0]
    return (int(major), int(minor or 0))


def _load_catalog_ids():
    """All catalog model ids (across routing providers), de-duped."""
    try:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            cat = json.load(f)
    except Exception:
        return []
    ids = set()
    for prov in (cat.get("providers") or {}).values():
        for m in prov.get("models", []):
            mid = m.get("id") if isinstance(m, dict) else m
            if mid:
                ids.add(mid)
    return sorted(ids)


def _normalize(vendor: str, catalog_id: str) -> str:
    """Map a catalog id (e.g. 'anthropic/claude-opus-4.8') to the hermes-native
    id form (e.g. 'claude-opus-4-8')."""
    base = catalog_id.split("/", 1)[1] if "/" in catalog_id else catalog_id
    base = re.sub(r":free$", "", base)
    if vendor == "anthropic":
        return base.replace(".", "-")          # claude-opus-4.8 -> claude-opus-4-8
    if vendor == "minimax":
        return "MiniMax-M3" if "m3" in base.lower() else base
    return base                                # google etc. keep as-is


def _best_id(vendor: str, family: str, catalog_ids):
    """Highest-version catalog id for (vendor, family), normalized; else safe default."""
    fam = family.lower()
    cands = []
    for cid in catalog_ids:
        low = cid.lower()
        if not low.startswith(vendor + "/"):
            continue
        if fam not in low:
            continue
        if ":free" in low:          # never auto-pick a free/preview-throttled tier for validation
            continue
        cands.append(cid)
    if cands:
        best = max(cands, key=_version_key)
        return _normalize(vendor, best)
    return SAFE_DEFAULTS.get((vendor, family))


def resolve_chain(tier: str):
    # explicit env override wins
    env = os.environ.get(f"VALIDATOR_{tier.upper()}_CHAIN", "").strip()
    if env:
        chain = []
        for spec in env.split(","):
            spec = spec.strip()
            if ":" in spec:
                prov, model = spec.split(":", 1)
                chain.append({"provider": prov.strip(), "model": model.strip(),
                              "label": f"{prov.strip()}:{model.strip()} (env)"})
        if chain:
            return chain
    catalog_ids = _load_catalog_ids()
    chain = []
    for vendor, family in PREFERENCES.get(tier, []):
        model = _best_id(vendor, family, catalog_ids)
        if not model:
            continue
        chain.append({"provider": VENDOR_PROVIDER.get(vendor, vendor),
                      "model": model,
                      "label": f"{vendor}/{family}->{model}"})
    return chain


if __name__ == "__main__":
    import sys
    tier = sys.argv[1] if len(sys.argv) > 1 else "high"
    print(json.dumps(resolve_chain(tier), indent=2))
