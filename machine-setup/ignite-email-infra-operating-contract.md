# ignite-email-infra poller operating contract

The PurelyMail notify-me poller is an externally owned deployment.  Hermes
registers only its cron schedule; `ignite-email-infra` owns the immutable poller
bytes, manifest, staging directory, deployment receipt, and rollback.

The deployer must stage a complete candidate beneath
`~/.hermes/deploy/purelymail-poller/`, verify the candidate's immutable
manifest and hashes, then atomically promote the staged release.  The cron
entry at `~/.hermes/scripts/purelymail-notify-poller.py` must resolve to the
verified staged artifact, never to hand-edited live bytes.  Rollback selects
the prior verified staged artifact through the external deployer; Hermes
operators must not repair the poller by copying or editing files in
`~/.hermes/scripts/`.

The current deployer-held lock is
`~/.hermes/deploy/purelymail-poller/.lock`.  Its cross-repository ownership,
stale-owner recovery, and the exact relationship between that deployment lock
and the Hermes cron invocation are not yet proven by this repository.  That is
an explicit lock-contract gap tracked by ClickUp task `86e2kmud6`; do not infer
that the Hermes cron lock protects external deployment.
