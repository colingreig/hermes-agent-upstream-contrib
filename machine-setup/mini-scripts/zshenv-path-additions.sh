#!/bin/bash
# Durable mirror of PATH-affecting lines added to ~/.zshenv (which is OUTSIDE
# restic backup coverage). ~/.zshenv itself is NOT restored by the mini
# off-box backup (offbox_restic_backup.py BACKUP_TARGETS); this file lives
# under ~/.hermes/scripts, which IS covered, so a rebuild can replay these
# lines back into ~/.zshenv instead of silently losing non-interactive gh/git
# auth again. Keep in sync by hand when ~/.zshenv PATH exports change.
#
# Added 2026-07-24 during the GitHub App gh CLI wrapper non-interactive auth
# repair (wrapper mints a fresh installation token per call; see
# ~/.hermes/bin/gh and ~/.hermes/scripts/github_app_cred.sh).

# --- BEGIN mirrored ~/.zshenv content ---
# Hermes GitHub App gh CLI wrapper (mints a fresh installation token per
# invocation via op-run; never stores a static token). Added 2026-07-24
# during git/gh non-interactive auth repair.
export PATH="$HOME/.hermes/bin:$PATH"
# --- END mirrored ~/.zshenv content ---
