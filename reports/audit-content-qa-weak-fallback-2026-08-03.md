# Weak-model content-QA CLI failure audit — 2026-08-03

Task: `86e2kxk4v`

## Scope and evidence

Primary evidence is Hermes session `cron_62714b869845_20260802_220015` (model `zai/glm-4.7`) and the matching `~/.hermes/logs/agent.log` / `errors.log` entries from 2026-08-02 22:07–22:09. Session message IDs below preserve the exact commands and outputs; log lines preserve runtime/model attribution.

The CLI implementations are owned by the external Ignite plugin, not this repository. The observed failures were not parser incompatibilities: every failing command expanded a shell variable in the same simple command that assigned it. POSIX shell expansion happens before that assignment enters the command environment, so each CLI received an empty value for a required argument and correctly printed usage.

## Observed failure modes

1. **Target-repository resolver received empty required paths three times.**
   - Session messages `75537`, `75539`, and `75541` used forms such as `TASK_JSON_FILE="..." node ... --task-json "$TASK_JSON_FILE"` and `TASK_REPO_ROOT="..." ... --repo-root "$TASK_REPO_ROOT"`.
   - In each simple command, the referenced variable expanded before its leading assignment took effect. Messages `75538`, `75540`, and `75542` returned `target-repo: usage...` with exit 2.
   - The model retried the same semantic error three times, triggering the tool-loop warning.

2. **The policy-mode wrapper received an empty `--root` twice.**
   - Messages `75547` and `75549` used `CONTENT_EVIDENCE_ROOT="..." node ... --root "$CONTENT_EVIDENCE_ROOT" --mode probe`.
   - Messages `75548` and `75550` returned `content-qa-command: usage...` with exit 2; the second was an exact duplicate and triggered `repeated_exact_failure_warning`.
   - Corroboration: `agent.log` lines 1360/1364 and `errors.log` lines 12516/12517, while surrounding API lines attribute the run to `zai/glm-4.7`.

3. **The direct engine received the same empty `--root` three times.**
   - Messages `75555`, `75557`, and `75559` repeated the inline-assignment form; the third only added `--policy-timeout-ms 60000`, which could not repair the missing root.
   - Messages `75556`, `75558`, and `75560` returned `content-qa-engine: usage...` with exit 2 and escalated from duplicate to same-tool-loop warnings.
   - Corroboration: `agent.log` lines 1440/1444/1448 and `errors.log` lines 12518–12520.

4. **Recovery bypassed the wrapper instead of diagnosing the shell expansion error.**
   - After the wrapper failures, the model checked file existence and invoked `content-qa-engine.mjs` directly (messages `75551`–`75560`). This discarded the wrapper's policy/legacy routing rather than correcting argument construction.

5. **A replacement evidence worktree was leaked after the failed probe.**
   - The first evidence worktree was removed at messages `75561`–`75562`.
   - Message `75563` created `/.../ignite-evidence-UlihaR`; no corresponding remove appears before the session ended. The gate therefore left temporary state behind after failure.

6. **The failed usage run was misreported as a repository capability failure.**
   - Final message `75568` classified the task as `unsupported content QA capability`, added `content-qa-unready`, and claimed a diagnostic comment, even though the repository config and engine had both been shown to exist (messages `75551`–`75554`) and no valid probe had run.

## Root fix

Weighted content-lane dispatch now forces the per-run `no_fallback` pin before provider authentication and before `AIAgent.fallback_model` is assembled. The primary configured tier can run the gate; if its authentication is unavailable, the content run fails closed instead of falling through to GLM-4.7. Weighted code dispatches retain their configured fallback policy. The persisted cron record is not mutated.

This boundary fix is stronger than weakening the CLIs to guess missing values: empty required paths are ambiguous and must continue to fail closed, and the affected CLI source is external to this repository.
