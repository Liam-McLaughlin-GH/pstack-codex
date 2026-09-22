---
name: make-bot-ui
description: Build a local button UI that relays JSON to a user-supplied webhook while keeping credentials server-side and reporting unavailable runtime controls. Use when a local page should wake an external bot or webhook.
---

# Make bot UI

Build a small page that a user can click. A server on this computer POSTs one
JSON object to a webhook. Keep the webhook URL and sender key on the server.
The browser must never receive the key.

This is a Codex-native adaptation of the upstream pstack skill at commit
`53e579f1481697931fc44f5445171397cfa2b24b`, documented in the [upstream
make-bot-ui skill](https://github.com/cursor/plugins/tree/53e579f1481697931fc44f5445171397cfa2b24b/pstack/skills/make-bot-ui/SKILL.md).
The upstream routine creation, routine-panel credential copy, and chat secret
request are Cursor-only controls. Codex cannot perform them, so this skill
requires a user-provided webhook URL and key through local environment
configuration. Do not invent a routine adapter, accept a key in chat, or put a
key in a page, screenshot, response, or log.

## Local relay

Use [scripts/serve.py](scripts/serve.py) as the reference implementation when
the target project does not already have a local server. It serves a static
page and relays the page's small payload:

```json
{"action": "status", "message": "status check"}
```

The receiving routine or webhook contract must name the same fields. Keep the
field list small and treat every received value as untrusted data.

Configure the relay in the local process environment, never in committed
files:

```sh
export MAKE_BOT_UI_WEBHOOK_URL='https://example.invalid/webhook'
export MAKE_BOT_UI_WEBHOOK_KEY='set-this-locally'
export MAKE_BOT_UI_TRIGGER_TOKEN='set-a-separate-local-token'
python3 skills/make-bot-ui/scripts/serve.py --port 8765
```

`MAKE_BOT_UI_TRIGGER_TOKEN` protects `/send`. The page receives it only as an
HttpOnly, same-origin cookie, and direct callers must send
`Authorization: Bearer <trigger-token>`. Missing or incorrect authorization is
rejected before the payload is read or relayed. Keep this token separate from
the webhook sender key.

The server binds to `0.0.0.0` by default so an already-connected tailnet peer
can reach it. Its outbound request uses:

- `POST` and `Content-Type: application/json`;
- `Authorization: Bearer <key>` and `X-Automation-Key: <key>`;
- one JSON object containing the declared fields;
- an 8-second timeout and no retry.

Webhook URLs must use HTTPS. For a loopback fixture only, pass
`--allow-local-http`; non-loopback HTTP remains rejected even with that flag.

HTTP 200 is the successful wake response. If the request fails, append the
same JSON payload to the configured local failure log. The log is for a
receiving routine or operator to drain later; it must not contain credentials
or response bodies. Do not send media bytes through this webhook.

Before calling the UI live, send one harmless payload that the receiving
workflow ignores. Report the observed HTTP status. Do not claim that a bot was
woken when the webhook was not reachable.

## Browser adapter and fallback

Use `python3 skills/make-bot-ui/scripts/capabilities.py` to select the local
browser adapter. The preferred adapter is `playwright-cli`, which supports
repeatable snapshots, clicks, and screenshots. If it is unavailable, the
helper selects a manual browser fallback. Open the printed local URL manually
and record the interaction result. If neither adapter can be used, stop at
`NOT RUN` and preserve the reason; do not fabricate screenshot or interaction
evidence.

For browser proof, exercise a harmless button and capture:

1. the page before the click;
2. the click that sends the declared JSON fields;
3. the visible success or failure status;
4. a screenshot with no URL, key, cookie, or other secret in view.

## Tailnet exposure

Check `tailscale status` and `tailscale ip -4` after the local fixture works.
If an online node is already present, use its reported hostname and IPv4
address and give the user HTTP URLs for both. Do not invent a hostname or IP.
If the client is missing or logged out, report tailnet exposure as
`UNSUPPORTED` or `NOT RUN` and leave installation, login, and credential entry
to the user. The local browser URL remains the only verified URL.

## Boundaries

| Upstream behavior | Codex treatment |
| --- | --- |
| Create a webhook routine | `UNSUPPORTED`: use a user-supplied compatible endpoint. |
| Copy a routine-panel URL and sender key | `UNSUPPORTED`: read local environment configuration only. |
| Ask the chat surface for a secret | `UNSUPPORTED`: never request or accept the key in chat. |
| Wake and parse a routine event | The external receiver owns this. Send the declared JSON body and document its field contract. |
| Serve over Tailscale | Conditional on an already-online local client; otherwise report the boundary. |

The repository remains MIT licensed. Preserve the upstream source reference
above when adapting this workflow, and do not copy private credentials or
runtime state into the skill.
