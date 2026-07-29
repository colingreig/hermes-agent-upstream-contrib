#!/usr/bin/env python3
"""
db_site_config.py — the site-config registry + routing/guard for the DB-backed publish lane.

WHY THIS EXISTS (2026-06-27 hardening)
--------------------------------------
The DB-backed publish lane (a Neon `posts` row write IS the publish — no PR/git deploy) was
HARDCODED single-target to dynamics365group.com: `LIVE_URL_BASE`, the writer-prompt site name,
the `/blog/<slug>` slug derivation, the `posts` table, the column whitelist, and the implicit
single `DATABASE_URL[_UNPOOLED]` all assumed that ONE site. A DB-publish task for a DIFFERENT
site would mis-route (wrong live URL / wrong DB / wrong schema) → either a write against the
WRONG database, or a permanent fail-closed live-URL retry loop = a task wedged forever. That is
exactly the strand class the lane rework was meant to kill.

THIS MODULE is the single source of truth:
  * SITE_CONFIG  — one entry per supported DB-backed site (domain → publish parameters).
  * site_for_domain() / site_for_task() — route a task to its SiteConfig (or None).
  * domain_from_url() / derive_domain_from_task() — the documented site-detection heuristic.
  * guard_resolve_site() — the FAIL-SAFE-LOUD entrypoint: returns either a resolved SiteConfig
    or a structured "park" verdict. Callers MUST park (ok:false, leave 'to do', comment) on a
    park verdict and NEVER process an unknown site against the wrong DB/domain, and NEVER enter
    the live-URL retry loop for it.

INVARIANT: an unrecognized DB-publish target is SURFACED (parked loudly, claimable), never
silently mis-routed and never silently stuck.

ADDING A NEW SITE: append one SITE_CONFIG entry with its domain, live_url_base, the env var
holding its connection string (db_url_env), its content table, the allowed content columns, and
its slug→URL pattern. No other file needs to change — every db_* script imports from here.
"""
import re

# ---------------------------------------------------------------------------
# Registry. ONE entry per DB-backed publish site. Seeded with dynamics365group.com
# reproducing TODAY's exact behavior byte-for-byte (live_url_base, posts table,
# the 7-column whitelist, the /blog/<slug> pattern, the default DATABASE_URL env).
# ---------------------------------------------------------------------------
class SiteConfig:
    """One DB-backed publish target. Plain value object (no behavior beyond helpers)."""
    __slots__ = ("domain", "live_url_base", "db_url_env", "table",
                 "allowed_cols", "slug_url_pattern", "publication_name")

    def __init__(self, domain, live_url_base, db_url_env, table, allowed_cols,
                 slug_url_pattern, publication_name):
        self.domain = domain
        self.live_url_base = live_url_base          # e.g. "https://dynamics365group.com/blog/"
        self.db_url_env = db_url_env                # ordered list: first set env wins (e.g. UNPOOLED then pooled)
        self.table = table                          # content table, e.g. "posts"
        self.allowed_cols = list(allowed_cols)      # the content-column whitelist for the writer prompt
        self.slug_url_pattern = slug_url_pattern    # compiled regex w/ a 'slug' group, applied to the task body
        self.publication_name = publication_name    # human site descriptor for the writer prompt

    def live_url(self, slug):
        return self.live_url_base + slug

    def as_dict(self):
        return {"domain": self.domain, "live_url_base": self.live_url_base,
                "db_url_env": self.db_url_env, "table": self.table,
                "allowed_cols": self.allowed_cols,
                "slug_url_pattern": self.slug_url_pattern.pattern,
                "publication_name": self.publication_name}


SITE_CONFIG = {
    "dynamics365group.com": SiteConfig(
        domain="dynamics365group.com",
        live_url_base="https://dynamics365group.com/blog/",
        # UNPOOLED preferred (direct transactional), pooled fallback. Was generic
        # DATABASE_URL[_UNPOOLED] injected by the old `doppler run -p claude-code -c dev`
        # wrapper; since Doppler's decommission (2026-07-03) these are sourced from
        # 1Password under the site-prefixed names (op://Dev Toolbox/dev/D365GROUP_*),
        # already present in the gateway's ambient env via op-secrets.env.
        db_url_env=["D365GROUP_DATABASE_URL_UNPOOLED", "D365GROUP_DATABASE_URL"],
        table="posts",
        # Exact 7-column whitelist db_apply has always permitted for `posts`.
        allowed_cols=["title", "description", "content", "excerpt", "category", "tags", "faq_data"],
        # Today's /blog/<slug> derivation (db_publish_task.derive_slug regex, verbatim).
        slug_url_pattern=re.compile(r"/blog/(?P<slug>[a-z0-9][a-z0-9\-]+)"),
        publication_name="dynamics365group.com (a Microsoft Dynamics 365 publication)",
    ),
}


# ---------------------------------------------------------------------------
# Site-detection heuristic (documented).
#
#   1. Scan the task body (description + text_content + name) for the FIRST http(s)
#      URL whose host matches a SITE_CONFIG domain (or a subdomain of one). That host
#      is the target site. This is robust: a DB-publish task always names the live
#      post URL it is rewriting, so the domain is in the body.
#   2. If no configured-domain URL is present, fall back to the FIRST http(s) URL's
#      registrable host as the "derived domain" — used ONLY to produce a clear
#      "unsupported target: <domain>" park reason (it will not match SITE_CONFIG).
#   3. If NO URL at all is derivable → domain is None → park as 'needs target site'
#      (do NOT guess a site silently — guessing is how a task gets mis-routed).
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")


def _task_blob(task):
    if not task:
        return ""
    return " ".join(str(task.get(k, "") or "")
                    for k in ("description", "text_content", "name"))


def domain_from_url(url):
    """Return the lowercased host of a URL, stripping a leading 'www.', else None."""
    if not url:
        return None
    m = _URL_RE.match(url) or _URL_RE.search(url)
    if not m:
        return None
    host = m.group(1).lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def site_for_domain(domain):
    """Exact or subdomain match against SITE_CONFIG. Returns SiteConfig or None."""
    if not domain:
        return None
    d = domain.lower().strip(".")
    if d.startswith("www."):
        d = d[4:]
    if d in SITE_CONFIG:
        return SITE_CONFIG[d]
    # subdomain match: blog.example.com -> example.com
    for cfg_domain, cfg in SITE_CONFIG.items():
        if d == cfg_domain or d.endswith("." + cfg_domain):
            return cfg
    return None


def derive_domain_from_task(task):
    """Apply the heuristic: return (domain, matched_config_bool).

    domain is the best-guess target host (a configured domain if one appears in the
    body, else the first URL's host, else None). The bool says whether it matched
    SITE_CONFIG. None domain => no derivable target site at all."""
    blob = _task_blob(task)
    if not blob:
        return None, False
    # Prefer a URL whose host IS a configured site (handles bodies that mention
    # several URLs, e.g. an internal-link list, where only one is the target site).
    for host in _URL_RE.findall(blob):
        cfg = site_for_domain(host)
        if cfg:
            return cfg.domain, True
    # No configured-domain URL — take the first URL's host for a clear park reason.
    first = _URL_RE.search(blob)
    if first:
        host = first.group(1).lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        return host, False
    return None, False


def site_for_task(task):
    """Route a task to its SiteConfig, or None if unconfigured/underivable."""
    domain, matched = derive_domain_from_task(task)
    return site_for_domain(domain) if matched else None


# ---------------------------------------------------------------------------
# THE FAIL-SAFE-LOUD GUARD.
# ---------------------------------------------------------------------------
def guard_resolve_site(task):
    """Resolve a task to its SiteConfig, fail-LOUD on an unconfigured target.

    Returns (cfg, park). EXACTLY ONE is non-None:
      * (SiteConfig, None) — supported site; proceed (publish/closeout as normal).
      * (None, park_dict)  — UNSUPPORTED/underivable target. The caller MUST:
          - NOT process the task against any DB/domain,
          - NOT enter the live-URL retry loop,
          - leave the task 'to do' (claimable, NOT wedged 'in progress'),
          - surface park_dict['reason'] (ok:false) and post park_dict['comment'].

    park_dict = {ok:false, stage, park:true, domain, reason, comment, supported_domains}
    """
    domain, matched = derive_domain_from_task(task)
    if matched:
        cfg = site_for_domain(domain)
        if cfg:
            return cfg, None
    supported = sorted(SITE_CONFIG.keys())
    if domain is None:
        reason = ("no target site derivable from the task body (no http(s) URL found) "
                  "— park as 'needs target site'")
        comment = ("\U0001F916 DB-publish lane: could NOT determine a target site for this task "
                   "(no post URL in the body). This lane routes by the live post URL's domain. "
                   "Add the target post's URL to the task, or — if this is a new DB-backed site — "
                   "add a SITE_CONFIG entry in ~/.hermes/scripts/db_site_config.py. Left 'to do' "
                   f"(claimable, not stuck). Configured sites: {', '.join(supported)}.")
    else:
        reason = (f"unsupported DB-publish target: {domain} (no SITE_CONFIG entry)")
        comment = ("\U0001F916 DB-publish lane: target site '" + domain + "' is NOT configured for "
                   "the DB-backed publish lane (no SITE_CONFIG entry), so this task was NOT "
                   "processed (refusing to mis-route against the wrong database/domain). To support "
                   "it, add a SITE_CONFIG entry for '" + domain + "' in "
                   "~/.hermes/scripts/db_site_config.py (domain, live_url_base, db_url_env, table, "
                   "allowed_cols, slug_url_pattern). Left 'to do' (claimable, not stuck). "
                   f"Configured sites: {', '.join(supported)}.")
    park = {"ok": False, "stage": "site_route", "park": True, "domain": domain,
            "reason": reason, "comment": comment, "supported_domains": supported}
    return None, park


def derive_slug(task, cfg):
    """Pull the slug from the task body using the SITE's slug_url_pattern. Returns slug or None."""
    blob = _task_blob(task)
    m = cfg.slug_url_pattern.search(blob)
    if not m:
        return None
    try:
        return m.group("slug")
    except Exception:
        # pattern without a named group — fall back to group(1)
        return m.group(1) if m.groups() else None
