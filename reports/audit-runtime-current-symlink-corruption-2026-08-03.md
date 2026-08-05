# Audit — `runtime-current` symlink corruption on the Mini

**Date:** 2026-08-03 · **Task:** ClickUp 86e2kt3yr · **Scope:** forensic audit of the
bare-name `~/.hermes/runtime-current` symlink found 2026-08-02 ~06:04 PDT, plus the
hardening it justifies.

**Verdict:** the governed cutter did not do it, and structurally cannot. The write came
from outside every registered and unregistered code path on the box — an ad-hoc
`ln -s <basename> runtime-current` executed with `cwd=~/.hermes` during the 2026-08-02
disk-recovery session. Confidence: high. No corruption is present today.

---

## 1. Incident reconstruction

`~/.hermes/runtime-current` is expected to be an absolute symlink to a direct child of
`~/.hermes/releases/`. It was found holding the bare name `v0.18.2-aae777c146da`. A
relative symlink resolves against the **link's own directory** (`~/.hermes`), not
`releases/`, so the pointer was **dangling**: `~/.hermes/v0.18.2-aae777c146da` does not
exist.

### Timeline (all PDT)

| Time | Event | Evidence |
|---|---|---|
| 08-01 20:40 | `dae2f24747` merged (#275) | git log |
| 08-02 01:47:44 | governed cut `dae2f247472a` → `0e9fdd43780f` | receipt `599b8982…`, `runtime_target` absolute |
| 08-02 02:33 | `aae777c146` merged (#283) | git log |
| 08-02 02:43:00 | governed cut `0e9fdd43780f` → **`aae777c146da`** | receipt `34c0ba78…`, `runtime_target=/Users/colingreig/.hermes/releases/v0.18.2-aae777c146da` (absolute), cut verified gateway health before exiting |
| 08-02 02:43 → 06:04 | **no receipt of any kind** — no cut, no rollback, no poll activation | receipt inventory (next receipt is 06:12:00) |
| 08-02 06:00:32 | cron agent `cron_f23a03e9d1b2_20260802_060032` starts | `~/.hermes/logs/errors.log` |
| 08-02 06:03:30 | disk-recovery handoff committed (`b9b51b4524`), stating "`runtime-current` still `v0.18.2-dae2f247472a`" — a **stale** reading, two governed cuts out of date | `.claude/handoffs/2026-08-02-hermes-disk-recovery-handoff.md:24` |
| 08-02 **06:04:13** | first hard failure: `/…/runtime-current/venv/bin/python: No such file or directory`, exit 127 | errors.log:6032 |
| 08-02 06:04:13 → **06:07:10** | ~30 `shell hook failed … command not found` warnings for `merge_guard.py` and `git_commit_identity_guard.py` | errors.log:6032-6061 |
| 08-02 ~06:04 | corruption spotted by a human; pointer restored to the receipt-verified absolute path | task description |
| 08-02 06:12:00 | governed cut `aae777c146da` → `b9b51b4524e5` succeeds, `from_commit=aae777c146da` — proving the pointer resolved correctly again by then | receipt `a4b2b9c8…` |

### Blast radius

Observed impact is bounded to **06:04:13 → 06:07:10** (~3 minutes) and to *newly spawned*
processes. Already-running services (the gateway) were unaffected: their interpreter path
was resolved at exec time, which is why the gateway kept serving the correct release while
the pointer was broken.

The worst consequence was not the exit-127: it was that the gateway's **pre-tool-call shell
hooks fail open**. `merge_guard.py` and `git_commit_identity_guard.py` are invoked through
`$HERMES_HOME/runtime-current/venv/bin/python`; when that path vanished, both were logged as
`WARNING … command not found` and the agent's tool call proceeded ungated. A dangling
`runtime-current` therefore silently disables the merge guard and the commit-identity guard
for every agent started in the window. That is a second finding, independent of the symlink
itself, and is filed as follow-up work rather than fixed here.

---

## 2. Was it the governed cutter? No — ruled out on code, on receipts, and on cadence

**Code.** `scripts/mini-release-cut.sh` writes the pointer in exactly one function,
`repoint_symlink()`, reached from exactly two call sites (the cutover main flow and
`rollback_to_previous`). Every target passes `assert_release_target()`, which requires
`target == "$RELEASES_DIR/$base"` where `RELEASES_DIR="$HERMES_HOME/releases"` is always
absolute. Targets originate from `release_target()` (absolute by construction) or from
`readlink`/`.previous` (both absolute). The swap is `ln -sfn "$target" "$tmp"` followed by
`mv -fh "$tmp" "$CURRENT_LINK"` — a same-filesystem `rename(2)`, i.e. atomic — and the
function re-reads `readlink` afterwards and dies on mismatch. **There is no reachable path
by which the cutter emits a relative or bare-name link.**

**Receipts.** A real cut, rollback or poll activation always writes a receipt. The receipt
inventory has a hard gap between 02:43:00 and 06:12:00. Absence of a receipt across the
corruption window is affirmative evidence that no governed actor touched the pointer.

**Registry.** `machine-setup/fleet-config/PRODUCTION_WRITERS.md` lists `mini-release-cut` as
the *sole* registered writer of the `runtime-release` resource
(`~/.hermes/releases/` + `~/.hermes/runtime-current`). This write was therefore
**out-of-band and unregistered**.

**No other writer exists.** A full sweep of the repo (bash, python, plists, json) and of the
live mini (`~/.hermes/scripts` — 439 files including `.bak`s, `~/.hermes/bin`,
`~/Library/LaunchAgents`) found **zero** other code that creates, moves, removes or
repoints `runtime-current`. Every other reference dereferences it read-only
(`gateway_launch_inner.sh`, `sentinel_run.sh`, `mini-release-poll.sh`,
`reconcile_launchd_environment.py`, `verify_governed_paths.py`, …). So the "suspect a
non-cutter writer" hypothesis resolves to: there is no such program — it was a human or an
agent at a shell.

---

## 3. Root cause (best-evidence hypothesis, high confidence)

An ad-hoc `ln -s v0.18.2-aae777c146da runtime-current` (or `ln -sfn … ~/.hermes/runtime-current`)
run with `cwd=~/.hermes` during the 2026-08-02 disk-recovery session.

Supporting evidence:

1. **The basename was correct, the prefix was missing.** The corrupt link named
   `v0.18.2-aae777c146da` — precisely the then-current governed target. Any code path that
   derives the target from `readlink` or `release_target()` yields an absolute path; only a
   path *typed from a listing* loses the prefix. `ls ~/.hermes/releases` prints bare names,
   which is exactly the shape that was written.
2. **A concurrent ad-hoc session had mini shell access in the window and was reasoning about
   this pointer.** The disk-recovery handoff (committed 06:03:30, ~30 seconds before the
   first failure) explicitly names `runtime-current` and asserts a value two cuts stale.
   An operator acting on that stale reading would try to "fix" the pointer.
3. **No poller was running.** On 2026-08-02 the release-poll LaunchAgent was absent (it was
   reinstated 2026-08-03); receipts that day are irregular (01:47, 02:43, 06:12, 08:15,
   11:35 …) versus the 15-minute cadence visible on 2026-08-03. All 08-02 cuts were
   ad-hoc/agent-driven, so ad-hoc shell activity against release state is corroborated
   independently.
4. **The gateway never restarted.** It kept serving the correct release throughout, which is
   consistent with a pointer flipped underneath a live process and inconsistent with any
   cut/rollback (both restart services).

**Why shell history does not confirm it:** `~/.hermes` is on a box whose agent sessions run
non-interactively (`claude --dangerously-skip-permissions`, `ssh mini '<cmd>'`). Those never
touch `~/.zsh_history`, which holds only 33 interactive lines and no `ln`. Absence there is
expected and is not counter-evidence. The two release directories involved
(`v0.18.2-aae777c146da`, `v0.18.2-dae2f247472a`) have since been pruned, so no on-disk
artifact of the incident survives.

**Residual uncertainty:** the handoff session believed the pointer was `dae2f247472a` while
the corrupt link named `aae777c146da`. The reconciliation is that the operator re-read state
(e.g. `ls ~/.hermes/releases`, or the last receipt) before writing, landing on the correct
*name* while dropping the *path*. This is the one link in the chain that is inferred rather
than observed.

**Rollback question (from the brief):** no `--rollback` occurred. The 08-02 receipts are all
`event: cut`, each `from_commit` chaining to the previous `to_commit`.

---

## 4. Gaps this exposed

| # | Gap | Status |
|---|---|---|
| G1 | Nothing asserted pointer health **between** cuts. `verify_governed_paths.py` covers it, but only runs *inside* a cut and manually — never on a cadence. Detection was a human eyeball. | fixed |
| G2 | A corrupt pointer produced a **misleading** error. The cut's first dereference is `git -C "$CURRENT_LINK" rev-parse` for the lease bootstrap, so the operator sees `could not resolve active runtime commit for bootstrap lease` — naming neither the defect nor a fix. | fixed |
| G3 | **No supported repair.** `--rollback` also dereferences the pointer, so the one obvious recovery command was unusable in exactly the situation it was needed. The 08-02 repair was itself an unregistered `ln`. | fixed |
| G4 | `verify_governed_paths.py` validated the *resolved* path only. A relative link that happens to resolve (`releases/v0.18.2-x`) passed, despite the receipt contract recording an absolute `runtime_target`. | fixed |
| G5 | `repoint_symlink`'s post-swap check compared **target equality only**; a link whose text matched but whose form was relative would have been accepted. | fixed |
| G6 | `tests/scripts/test_mini_release_cut_safety.sh` — the cutter's only safety suite — was **not collected by pytest and not run by any workflow**. It could rot silently while CI stayed green. | partly fixed — now collected, but macOS-only (see below) |
| G7 | Gateway **shell hooks fail open** when their interpreter is unreachable: `merge_guard.py` and `git_commit_identity_guard.py` degraded to warnings and tool calls proceeded ungated. | follow-up |
| G8 | `PRODUCTION_WRITERS.md`'s unknown-path guard is documentation, not runtime admission control (its own follow-ups `86e2kmucr`/`86e2kmuct` are still open), so an out-of-band write is unpreventable by design today. | follow-up (already tracked) |

---

## 5. What changed

- **`current_link_health()` / `current_link_structure_ok()`** (`scripts/mini-release-cut.sh`):
  one definition of pointer health — is a symlink, raw link text is **absolute**, no `.`/`..`
  components, parent canonicalizes to `releases/`, resolves to a directory, and (full health)
  has an executable `venv/bin/python`. Checks the **raw** text, not just the resolved path.
  Repeated slashes are tolerated (cosmetic, no escape); the releases dir is compared after
  canonicalization so a `$HOME` under a symlinked prefix does not false-positive.
- **`--verify-pointer`**: read-only probe. Exit 0 + resolved release, or exit 1 + a one-line
  reason. No lock, no lease, no mutation. This is the probe contract for monitors and the
  first step of the runbook.
- **`--repair-pointer`**: repoints to the `runtime_target` recorded in
  `.mini-release-last-receipt.json`, only after that receipt byte-matches its
  content-addressed `.mini-release-receipt-<sha256>.json` twin and names an existing direct
  child of `releases/`. The operator does not choose the target. It takes the cut lock via
  the same exclusive `os.link(2)` primitive but **without** a production-write lease (which
  is structurally unobtainable while the pointer is broken) and **refuses** — never evicts —
  if a cutter already holds it. Uses the *system* `python3`, since the release venv is
  reached through the very pointer being repaired. Idempotent.
- **Fail-closed preflight**: `assert_current_link_healthy` now runs before the first
  dereference of `$CURRENT_LINK` in every mutating mode, so a corrupt pointer is named
  precisely (with the repair command) instead of surfacing as a lease-bootstrap error, and
  can never be copied into `PREV_TARGET`/`.previous`/a receipt.
- **Post-swap assertion**: `repoint_symlink` now re-asserts the full structural contract
  after its `mv`, not just target equality.
- **Detection cadence** (`scripts/mini-release-poll.sh`): the poller — which runs every 15
  minutes and is the fleet's most frequent toucher of this path — probes the pointer before
  anything else and fails closed with the stable prefix
  `mini-release-poll: runtime-current pointer corrupt: `. Its liveness heartbeat is still
  printed first, so the existing `com.colingreig.hermes.release-poll` liveness contract is
  unaffected. No new LaunchAgent, no `jobs.json` change.
- **Distinguishable alarm** (`fleet_outcome_contracts.json`): that prefix is registered as a
  `failure_patterns` entry for the release-poll outcome, so a corrupt pointer alarms as
  itself rather than as generic poller silence. `fleet_outcome_manifest.json` sha256s
  regenerated accordingly.
- **`verify_governed_paths.py`**: now rejects a relative `runtime-current` (even a
  resolvable one) and any `.`/`..` component in the raw link text. Deliberately *not* a
  string comparison against the resolved path — this verifier runs inside every cut, and a
  home under a symlinked prefix would otherwise fail a healthy pointer and roll the cut back.

### Tests

- `tests/scripts/test_mini_release_cut_safety.sh` — new block covering every corruption
  shape (missing, not-a-symlink, bare-name relative, resolvable relative, traversal,
  escaping, dangling, no venv), the healthy case, the structural-vs-full split,
  `assert_current_link_healthy`'s die-with-repair-hint, `repoint_symlink` rejecting a
  bare-name pointer written by a faulty `mv`, and `receipt_verified_runtime_target`'s
  content-addressing and out-of-releases refusals.
- `tests/scripts/test_mini_release_pointer_modes.py` — 8 end-to-end tests of
  `--verify-pointer` / `--repair-pointer` (read-only probe, receipt-verified repair,
  idempotence, lock release, refusal under a held lock, refusal on an unverifiable receipt,
  flag exclusivity).
- `tests/scripts/test_mini_release_poll_pointer_guard.py` — 10 tests: heartbeat-before-verdict,
  8 corruption shapes rejected with the greppable prefix, healthy pointer admitted.
- `tests/machine_setup/test_verify_governed_paths.py` — 3 new cases (bare-name relative,
  resolvable relative, non-canonical absolute).
- `tests/scripts/test_mini_release_cut_safety_suite.py` — collects the bash suite under
  pytest so it is finally on a gate (G6). It skips on non-Darwin: the suite
  exercises BSD-only cutter primitives, above all `mv -fh` in `repoint_symlink`
  (GNU coreutils rejects `-h`; its equivalent is `-T`). `-h` is load-bearing —
  without it BSD `mv` moves the staged link *into* the current release
  directory and the pointer is never swapped — so making the primitive
  portable to satisfy an Ubuntu runner would put that exact regression one bad
  platform detection away. The suite therefore runs on every macOS developer
  machine and on the mini, and reports as skipped on the Linux CI runner. Same
  gate for the two `--repair-pointer` tests that reach `repoint_symlink`; the
  read-only `--verify-pointer` tests and the whole poller-guard suite are
  cross-platform and do run in CI. **Residual gap: CI has no macOS job, so the
  bash suite still has no automated enforcement — closing that needs either a
  macOS runner or a portable swap primitive, and is a deliberate follow-up.**

---

## 6. Repair runbook

**Symptom.** Cron/agent output contains
`/Users/colingreig/.hermes/runtime-current/...: No such file or directory`, or
`shell hook failed (… command not found)`, or the release poller logs
`mini-release-poll: runtime-current pointer corrupt: …`.

1. **Diagnose (read-only, safe at any time):**
   ```sh
   ssh mini '~/.hermes/runtime-current/scripts/mini-release-cut.sh --verify-pointer'
   # if the pointer itself is unusable, run the cutter from a release dir directly:
   ssh mini 'bash ~/.hermes/releases/<latest>/scripts/mini-release-cut.sh --verify-pointer'
   ```
   Exit 0 prints the resolved release. Exit 1 prints one line naming the defect.

2. **Record the evidence** before repairing — the pointer is the only artifact:
   ```sh
   ssh mini 'ls -l ~/.hermes/runtime-current; readlink ~/.hermes/runtime-current; \
             stat -f "%N btime=%SB" -t "%F %T %z" ~/.hermes/runtime-current; \
             cat ~/.hermes/releases/.mini-release-last-receipt.json'
   ```

3. **Repair (receipt-derived, never hand-typed):**
   ```sh
   ssh mini 'bash ~/.hermes/releases/<latest>/scripts/mini-release-cut.sh --repair-pointer'
   ```
   Refuses if a cutter holds the release lock — wait for the cut to finish and re-diagnose.
   Refuses if the last receipt is unverifiable — in that case escalate rather than guessing a
   target; a hand-picked release can silently downgrade the fleet.

4. **Confirm**: `--verify-pointer` exits 0, then
   `python3 ~/.hermes/scripts/verify_governed_paths.py --home ~` exits 0.

5. **Restart only if a service actually failed during the window.** Repair does not restart
   anything; running services hold their pre-repair image and are usually healthy. Check
   `~/.hermes/logs/gateway.log` and `errors.log` for the outage window first.

6. **Never** `ln -s` the pointer by hand. A bare name resolves against `~/.hermes`, not
   `releases/`, which is precisely how 2026-08-02 happened. If `--repair-pointer` cannot be
   used, the correct fallback is an absolute link (`ln -sfn "$HOME/.hermes/releases/<dir>"
   "$HOME/.hermes/runtime-current"`) taken from the last receipt's `runtime_target`, followed
   by `--verify-pointer`.

---

## 7. Closing sanity check

`runtime-current` on the mini as of 2026-08-03 08:19 PDT:

```
lrwxr-xr-x  1 colingreig  staff  55 Aug  3 07:59
  /Users/colingreig/.hermes/runtime-current -> /Users/colingreig/.hermes/releases/v0.18.2-85f0c922e650
```

Absolute, canonical, a direct child of `releases/`, resolving to an existing directory, and
matching `runtime_target` in `.mini-release-last-receipt.json`
(receipt `47d89e85…`, `event=noop`, `to_commit=85f0c922e650…`). **Healthy.**

---

## 8. Follow-ups to file

- **Shell hooks fail open on a missing interpreter** (G7). `merge_guard.py` and
  `git_commit_identity_guard.py` degrade to `WARNING … command not found` and the tool call
  proceeds. Decide the policy (fail closed vs. alarm) and implement. *Security-relevant.*
- **Runtime admission control for `PRODUCTION_WRITERS.md`** (G8) — already tracked as
  `86e2kmucr` / `86e2kmuct`. This incident is a concrete argument for prioritising it: the
  registry documented a single writer while an unregistered write was trivially possible.
- **`fleet_outcome_manifest.json` has no standalone hash-regeneration CLI.** Editing any file
  it pins requires hand-recomputing sha256s; getting it wrong aborts and silently rolls back
  every cutover. The repo's fixture tests do catch drift, but a `--write-manifest-hashes`
  mode (as `install_disk_lifecycle.py` has) would remove the footgun.
