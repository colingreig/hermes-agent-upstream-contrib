#!/usr/bin/env python3
"""validator_image_authenticity.py — vision backstop that BLOCKs obviously
AI-fake generated VEHICLE images before the autonomous merge gate lands them.

WHY THIS EXISTS (2026-06-22):
  The jdmbuysell "Driven" Evo IV–VI image set passed the count/source tripwires
  (>=4 distinct, owned, not scraped) but was OBVIOUSLY AI-fake — empty cabins on
  moving cars, garbled RALLIART windshield banners, smeared license plates,
  missing Evo widebody fender flares, painterly grade. The executor QC only
  checked SUBJECT IDENTITY ("right car, distinct scenes"), never PHOTOREALISM.
  The primary fix is executor-side self-QC (skills/vehicle-image-qc). THIS is the
  validator BACKSTOP: a vision lens over the actual added image bytes.

HOW IT REUSES THE EXISTING VALIDATOR (no new model plumbing):
  - diff parsing            validator_common.parse_unified_diff
  - image bytes             `gh api repos/{repo}/contents/{path}?ref={sha}`
  - the vision call          the SAME one-shot the panel uses — `hermes -z PROMPT
                             -m <vision model> --provider <p>` — which reuses the
                             Hermes credential pool / failover AND auto-attaches a
                             local image path in the prompt as a vision part
                             (agent.image_routing.extract_image_refs).
  - blocking                 findings flow into validate_pr.py's existing
                             FAIL-CLOSED override (any HIGH finding -> BLOCK).

CALIBRATION (deliberate, opposite of the high-tier panel):
  This is a BACKSTOP on top of executor self-QC, and a FALSE block bricks the
  Driven pipeline. So it is biased to REAL: it raises a HIGH (blocking) finding
  ONLY when the model cites a CONCRETE, unambiguous fake tell. Any operational
  problem (no head sha, can't fetch bytes, model error, no parseable verdict) is
  a MEDIUM (warn, NON-blocking) finding — fail-OPEN, never brick the gate on a
  vision hiccup. Scope is narrow (only /images/driven/ assets) so non-jdmbuysell
  repos and non-vehicle PRs cost nothing.

run(diff_text, repo, head="", pr=None) -> {"findings": [{check,severity,file,detail}]}

Reversibility: delete this file + the ~6-line block in validate_pr.py that calls
run() and merges its findings (see the "image authenticity" comment there).
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

if __package__:
    from . import validator_common as vc
else:
    import validator_common as vc

# Vision model for the backstop. gemini-3.5-flash is vision-capable, cheap, and
# is already the configured auxiliary.vision backend (config.yaml) + the catalog
# safe-default for ("google","flash"). The chain falls back to a Claude vision
# model if google is unavailable; we keep it short on purpose (a backstop should
# be cheap). Override with VALIDATOR_IMAGE_CHAIN="provider:model,provider:model".
_DEFAULT_CHAIN = [
    ("google", "gemini-3.5-flash"),
    ("anthropic", "claude-haiku-4-5-20251001"),
]
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
VISION_TIMEOUT = 120          # seconds per image one-shot
MAX_IMAGES = 8                # cap cost/latency; warn (not block) if more
CACHE_DIR = os.path.expanduser("~/.hermes/cache/vehicle_qc")

# Owned editorial vehicle assets. jdmbuysell-specific path; harmless elsewhere
# (won't match), so this check is a no-op on every other repo.
_DRIVEN_IMAGE = re.compile(r"(^|/)images/driven/[^/]+\.(jpe?g|png|webp)$", re.I)


def _chain():
    env = os.environ.get("VALIDATOR_IMAGE_CHAIN", "").strip()
    if env:
        out = []
        for spec in env.split(","):
            spec = spec.strip()
            if ":" in spec:
                p, m = spec.split(":", 1)
                out.append((p.strip(), m.strip()))
        if out:
            return out
    return list(_DEFAULT_CHAIN)


def _driven_images_in_diff(files):
    """Paths of /images/driven/ image files TOUCHED by this PR (added or
    modified — a fix-forward that regenerates the set reuses filenames, so we
    must NOT require is_new_file). Order-preserving, de-duped."""
    seen, out = set(), []
    for fd in files:
        path = fd.get("path", "")
        if _DRIVEN_IMAGE.search(path) and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _fetch_image_bytes(repo, path, ref):
    """Fetch a file's bytes at `ref` via the gh contents API (base64). Returns
    a local cache file path, or None on any error (fail-open upstream)."""
    if not ref:
        return None
    try:
        # ref MUST go in the URL query string. `gh api -f ref=…` would switch the
        # request to POST and 404 the contents endpoint. -X GET keeps it a read.
        from urllib.parse import quote
        url = f"repos/{repo}/contents/{quote(path)}?ref={quote(ref)}"
        r = subprocess.run(
            ["gh", "api", "-X", "GET", url, "-q", ".content"],
            capture_output=True, text=True, timeout=vc.GH_TIMEOUT)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        raw = base64.b64decode(r.stdout)
        if not raw:
            return None
        os.makedirs(CACHE_DIR, exist_ok=True)
        local = os.path.join(CACHE_DIR, f"{ref[:8]}-{os.path.basename(path)}")
        with open(local, "wb") as f:
            f.write(raw)
        return local
    except Exception as e:
        print(f"[image-authenticity] fetch {path}@{ref[:8]} error: {e!r}",
              file=sys.stderr)
        return None


_PROMPT = """You are a forensic reviewer for an autonomous publishing gate. The \
attached image is a GENERATED editorial photo of a specific car for a car \
marketplace blog. Decide whether a human would see it as an OBVIOUS AI-generated \
fake, versus a believable photograph.

Flag it FAKE ONLY if you can point to a CONCRETE, unambiguous defect from this list:
- a car shown IN MOTION (driving / sliding / cornering) with NO visible driver or no hands on the wheel
- sharp, UNBLURRED wheels on a car that is clearly moving (no motion blur on a moving car)
- any badge, banner, sticker, or LICENSE PLATE rendered as garbled / illegible / melted text, or a smeared plate
- left-vs-right HEADLIGHTS or TAILLIGHTS that are asymmetric in shape or size
- smoke / dust / spray that does NOT originate at the tyre contact patch (a floating airbrushed puff beside the car)
- a named-model body that is clearly WRONG — e.g. a Mitsubishi Lancer Evo IV–VI MUST have wide blistered fender flares; a narrow-body sedan with a wing bolted on is wrong
- inconsistent wheel-spoke designs across the same car, or melting / morphing hard surfaces (grille, lights)
- scene lighting that flatly contradicts the lighting on the car

If you are UNCERTAIN, or it looks like a plausible real photograph, answer REAL. \
Do NOT flag merely for colour grade, mood, or being "too clean / cinematic" — \
only the concrete physical defects above.

Image: {image_path}

Reason in at most 4 lines, then end your reply with EXACTLY one final line, \
starting with the bare token VERDICT: — one of:
VERDICT: REAL
VERDICT: FAKE — <the single concrete tell you saw>
"""

_VERDICT_RE = re.compile(r"VERDICT\s*:\s*(.+)$", re.IGNORECASE)


def _parse(out):
    """(verdict, reason). verdict in {REAL, FAKE, ERROR}. Default ERROR when no
    verdict token at all — ERROR is non-blocking (fail-open), so an unparseable
    reply never falsely blocks a real photo."""
    verdict, reason = "ERROR", "no parseable verdict"
    for line in (out or "").splitlines():
        m = _VERDICT_RE.search(line)
        if not m:
            continue
        body = m.group(1).strip().strip("*`_ ").strip()
        up = body.upper()
        if up.startswith("REAL"):
            verdict, reason = "REAL", ""
        elif up.startswith("FAKE"):
            verdict = "FAKE"
            reason = body[4:].lstrip(" —-:*`").strip() or "AI-fake tell present"
    return verdict, reason


def _vision_verdict(image_path):
    """Run the image through the vision chain. Returns (verdict, reason, label).
    REAL/FAKE from a model that answered; ERROR if the whole chain failed."""
    prompt = _PROMPT.format(image_path=image_path)
    last = ("ERROR", "empty chain", "?")
    for provider, model in _chain():
        cmd = [HERMES_BIN, "-z", prompt, "-m", model, "--provider", provider]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=VISION_TIMEOUT)
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            if r.returncode != 0 and "VERDICT:" not in out:
                last = ("ERROR", f"hermes rc={r.returncode}: "
                        f"{(r.stderr or '').strip()[:140]}", f"{provider}/{model}")
                continue
            verdict, reason = _parse(out)
            if verdict in ("REAL", "FAKE"):
                return verdict, reason, f"{provider}/{model}"
            last = ("ERROR", reason, f"{provider}/{model}")
        except Exception as e:
            last = ("ERROR", f"{e!r}", f"{provider}/{model}")
    return last


def run(diff_text, repo="", head="", pr=None):
    files = vc.parse_unified_diff(diff_text)
    paths = _driven_images_in_diff(files)
    if not paths:
        return {"findings": []}

    ref = head or (f"refs/pull/{pr}/head" if pr else "")
    findings = []
    if not ref:
        # Can't resolve a ref to fetch the bytes -> warn, never block (fail-open).
        findings.append({"check": "vehicle_image_authenticity", "severity": "medium",
                         "file": paths[0],
                         "detail": "could not resolve a head ref to fetch image bytes "
                                   "for photorealism review (vision backstop skipped)"})
        return {"findings": findings}

    if len(paths) > MAX_IMAGES:
        findings.append({"check": "vehicle_image_authenticity", "severity": "medium",
                         "file": f"({len(paths)} driven images)",
                         "detail": f"only the first {MAX_IMAGES} of {len(paths)} "
                                   "driven images were vision-reviewed (cap)"})
        paths = paths[:MAX_IMAGES]

    for path in paths:
        local = _fetch_image_bytes(repo, path, ref)
        if not local:
            findings.append({"check": "vehicle_image_authenticity", "severity": "medium",
                             "file": path,
                             "detail": "could not fetch image bytes for photorealism "
                                       "review (vision backstop skipped for this file)"})
            continue
        verdict, reason, label = _vision_verdict(local)
        if verdict == "FAKE":
            findings.append({"check": "vehicle_image_authenticity", "severity": "high",
                             "file": path,
                             "detail": f"AI-fake vehicle image [{label}]: {reason}. "
                                       "Regenerate via the vehicle-image-qc reject-gate "
                                       "before merge."})
        elif verdict == "ERROR":
            findings.append({"check": "vehicle_image_authenticity", "severity": "medium",
                             "file": path,
                             "detail": f"photorealism review inconclusive [{label}]: "
                                       f"{reason} (fail-open, not blocking)"})
        # REAL -> no finding
    return {"findings": findings}


def main():
    p = argparse.ArgumentParser(description="Vision backstop: block AI-fake vehicle images.")
    vc.add_source_args(p)
    p.add_argument("--head", default="", help="head sha to fetch image bytes at")
    args = p.parse_args()
    diff_text, _ = vc.load_diff_from_args(args)
    repo = getattr(args, "repo", "") or ""
    head = args.head
    if not head and repo and getattr(args, "pr", None):
        head = vc.pr_head_sha(repo, args.pr)
    print(json.dumps(run(diff_text, repo=repo, head=head,
                         pr=getattr(args, "pr", None)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
