# Operator setup: safe personal agent (chief of staff + engineering lead)

What the template now does for you: runs the agent's shell in a Modal sandbox when
credentials are present, seeds approvals/deny rules, ignores strangers once you
set an allowlist, rate-limits the admin login, and seeds `SOUL.md`, `USER.md`
and nine starter skills (only if the files do not exist yet).

What it cannot do for you is below. Do these once.

## 1. Railway
- `ADMIN_PASSWORD`: 24+ random characters. Setup warns below 16.
- Attach a volume at `/data`.
- Put Cloudflare Access (or Tailscale) in front of the URL: the Hermes Console
  is a shell, so a leaked admin password is total compromise.

## 2. Access control
- Create a Telegram bot with BotFather. In Setup set `TELEGRAM_ALLOWED_USERS` to
  your numeric id and keep "Allow all users" **off**. Draft approvals arrive here.
- WhatsApp: `WHATSAPP_ALLOWED_USERS` = you plus only the groups it may answer in.
  The bridge is an unofficial client on your number and can get the number banned;
  consider a second number.

## 3. Sandbox
- Create a Modal account, set `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` in Setup, and
  set a monthly spend limit in the Modal dashboard.
- The sandbox receives only `GITHUB_TOKEN`. Verify after saving: ask the agent to
  run `cat /data/.hermes/.env`. It must fail inside the sandbox.

## 4. GitHub (full write, guarded)
- Fine-grained token, selected repositories only, Contents / Pull requests /
  Issues / Actions read+write, no admin or organisation scopes, 90-day expiry.
- On every repo protect `main`: require a pull request and passing CI, block force
  pushes and deletion, enable secret scanning + push protection. This is the real
  guard; approval prompts are skipped inside container backends.

## 5. ClickUp, Gmail, Calendar
- ClickUp: try the official MCP (`https://mcp.clickup.com/mcp`, OAuth). If it
  rejects Hermes as a client, use a ClickUp API token (`CLICKUP_API_KEY`). Use a
  dedicated "Hermes" ClickUp user so its actions are attributable.
- Gmail/Calendar: connect Hermes' Google Workspace OAuth from the dashboard
  Console. The Gmail scope can send mail, so "never email clients without
  approval" is a policy in `SOUL.md`, not a hard block.

## 6. Cron jobs (dashboard Console, deliver to Telegram)
- 07:30 Mon-Fri: `morning-brief`
- 09:00 and 16:00: `clickup-ops` follow-up digest
- Friday 17:00: `dev-lead` repo health + memory/voice review (propose, never apply)

## 7. Per-client isolation
- Default profile = you (Telegram, email, GitHub, internal ClickUp).
- One named profile per key client: `hermes profile create <client> --no-credentials`,
  then edit its `SOUL.md` (who they are, agreed prices, tone) in the dashboard's
  profile selector. `/setup` only manages the default profile.
- Route client WhatsApp chats with `gateway.multiplex_profiles: true` and
  `gateway.profile_routes` (platform + chat id -> profile). **Check this exists in
  the pinned Hermes version before relying on it** (the docs describe it; it has not
  been verified against v0.22.2). Client email is not routed: drafts must quote only
  that client's own thread.

## 8. Train your voice
Export from Gmail Sent (~150), four to six WhatsApp chats (.txt) and your ClickUp
comments (~200). Strip third-party personal data, distil rules plus 5-8 examples
per context into `skills/my-voice/SKILL.md`, then delete the raw samples.

## 9. Meetings
Turn on Google Meet / Zoom transcripts. The `meeting-notes` skill summarises them
and proposes ClickUp tasks and follow-up drafts. Mention recording at the start of
client meetings.

## 10. Roll out in stages
1. Week 1 read-only: brief, email and ClickUp summaries.
2. Week 2 drafts, approved on Telegram.
3. Week 3 teammate auto-replies; read the outbound log daily.
4. Week 4 GitHub write via branches and PRs.

## Limits to remember
Hard controls: sandbox, allowlist, branch protection, login limit. Soft controls
(rely on the model following `SOUL.md`): client approval, teammate scope, never
sending email. Use a strong model and set spend caps with your LLM provider.
