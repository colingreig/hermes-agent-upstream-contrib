#!/bin/bash
cat << "MSG"
🔐 *CODEX RE-LOGIN DUE (Thu)* — 5 Hermes crons paused since 2026-07-20

The mini lost its Codex OAuth in the 7/19 auth.json truncation; you said you could 2FA the ChatGPT account today. Steps:

1) On the mini (GUI Terminal — needs a real browser for the OAuth flow):
     codex login          # ChatGPT OAuth — do NOT use --with-api-key
     codex login status   # expect: Logged in

2) Resume the 5 paused openai-codex crons:
     export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
     for id in 62714b869845 a00cf8e420b1 5a76e290811d baa3251e033d 5cb355d136ce; do hermes cron resume $id; done
   (clickup-executor, email-triage, hermes-pr-validate, clickup-executor-2, hermes-self-report)

3) Verify: hermes cron list  → all 5 [active], no more "No Codex credentials stored" errors.
MSG
