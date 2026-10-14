# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Shelly is a personal AI assistant: Claude (via the Anthropic API) that the operator
talks to over Telegram. It is built to grow — new capabilities arrive as modules that
give Claude tools, without changes to the core. The front end and dashboard live in
`../Shelly-Web-App` (Flask, deployed on Railway at sulcata.ai); Shelly itself runs on
the Jetson.

Status: **v1 runs end to end over Telegram (@sulcata_ai_bot) as a systemd user
service on the Jetson.** The roadmap at the bottom is the work tracker —
keep it current as items land.

## Decisions

- **Python 3.13**, in `.venv/` (create with `/usr/local/bin/python3.13 -m venv .venv`;
  the system `python3` is 3.8). Official `anthropic` SDK, `python-telegram-bot`,
  `pymongo` (its async client), `python-dotenv`.
- **Runs on the Jetson**, receiving Telegram updates by long polling — no public URL,
  no webhook, and tools can reach the local machine.
- **One operator.** Only chats listed in `TELEGRAM_ALLOWED_CHAT_IDS` are answered;
  every other update is dropped silently (logged, never replied to). A Telegram bot is
  publicly discoverable, and once tools can act, an open bot is an open door.

## Running on the Jetson

Shelly is the systemd **user** service `shelly` (linger is on, so it runs without a
login and starts at boot). The unit's source is `deploy/shelly.service`; it is
*copied* to `~/.config/systemd/user/`, not linked, because the SSD mounts late and
a link into it would not load at boot. After editing it, copy it again and
`systemctl --user daemon-reload`.

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # needed from a non-login shell
systemctl --user status shelly
systemctl --user restart shelly              # after a code change
tail -f logs/shelly.log                      # stdout+stderr, appended
```

`Restart=always` after 10s, with no start limit: a crash comes back on its own,
and at boot it keeps retrying until the SSD and network are up. SIGTERM (stop,
restart) shuts the poller down cleanly. Never also run `python -m shelly` by hand
while the service is up: two pollers on one bot token fight over updates. Startup
takes ~5s on the Jetson, most of it imports.

## Environment (`.env`, gitignored)

| variable | meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | comma-separated chat ids Shelly will answer |
| `ANTHROPIC_API_KEY` | API key |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` |
| `ANTHROPIC_EFFORT` | `output_config.effort`; `medium` |
| `MONGODB_URI` | Atlas cluster |
| `MONGODB_DB` | optional, default `shelly` |
| `DASHBOARD_URL` | the web app, for links in replies (not used in v1) |

## Architecture

```
shelly/
  __main__.py        # load config, wire the pieces together, start polling
  config.py          # read and validate .env once, at startup
  core/
    agent.py         # one turn: history -> Claude -> tool loop -> reply text
    tools.py         # Tool (name, description, input schema, handler) and the registry
    prompt.py        # the system prompt
  memory/
    history.py       # the Mongo conversation store and the context window
  channels/
    telegram.py      # Telegram in/out: allowlist, formatting, splitting
  capabilities/      # one module per capability, each exporting its tools
tests/
```

Run with `.venv/bin/python -m shelly` (logs to stderr; `logs/` is gitignored for
redirected output). `shelly/config.py` reads `.env` once at
startup into a frozen `Config` and raises `ConfigError` listing every problem at
once; an empty or missing allowlist is an error, never "everyone". `Config`'s repr
leaves out the secrets, so it is safe to log. Tests are plain `unittest`:
`.venv/bin/python -m unittest discover -s tests -t . -v`.

**Layering.** The core knows nothing about Telegram, and channels know nothing about
Claude: a channel hands the core `(chat_id, text)` and gets text back. A second
channel (the web dashboard, say) plugs into the same core. Capabilities only add
tools; they never edit the loop.

**Capabilities.** A capability is a module in `capabilities/` that exports a list of
`Tool`s. The registry collects them at startup and the agent passes them all to
Claude. Adding a capability = adding a module + listing it in config. Tool handlers
are async, take the validated input, and return a string (or raise; the loop turns an
exception into a `tool_result` with `is_error: true` rather than crashing the turn).
Define tools with `strict: true` and a full JSON schema.

**The agent loop.** A manual loop over `client.messages.create` (or `.stream` +
`get_final_message()` once replies get long) using `AsyncAnthropic`: send, and while
`stop_reason == "tool_use"` run every `tool_use` block — concurrently — and return all
results in one user message. Within a turn, append the assistant's full `content`
(thinking blocks included) back unchanged; never rebuild it from text. Check
`stop_reason` before reading content: `refusal` and `max_tokens` get a plain apology
reply, not a crash. Cap tool iterations per turn (`MAX_STEPS`, 10).

`Agent.reply(chat_id, text)` is the core's whole interface: it records the user
message, builds the window, runs the loop and returns text to send — always text,
never an exception. Only a real reply is stored; an API error, a refusal (after
the fallback also declined), an empty reply or a loop cut off at `MAX_STEPS` is sent
as a canned message but not stored, so the next window joins the unanswered message
with the next one. A reply cut off at `max_tokens` is stored with a note saying so.
`Registry.run()` never raises: an unknown tool or a handler exception becomes an
`is_error` result Claude can see. Tools keep registration order so the tools
block stays byte-identical between requests. `tests/fakes.py` has the scripted
`FakeClaude` and `FakeHistory` the core tests run on.

Model notes for `claude-opus-5-5`: thinking is always on and cannot be disabled —
`effort` is the only dial (default would be `medium`; set it explicitly from `.env`);
`tool_choice` `any`/`tool` returns a 400, use `auto`; no assistant prefill. Enable the
server-side refusal fallback (`fallbacks: "default"` with beta
`server-side-fallback-2026-07-01`) so a refusal is retried on another model rather
than becoming a dead message.

**Conversation memory.** Collection `messages`, one document per message:
`{chat_id, role: "user"|"assistant", text, created_at}`, index
`(chat_id, created_at desc, _id desc)` — `_id` breaks same-millisecond ties.
Everything is kept; only the context window is limited.

- `History.add()` records, `History.window()` returns the API `messages` list, and
  `build_window()` is the pure function the repairs below live in (tested without
  Mongo). `connect(cfg)` returns the `AsyncMongoClient` (tz-aware) and the `History`.
- The window is the last `HISTORY_LIMIT` (20) messages for the chat — roughly ten
  from each side — oldest first.
- Only final text is stored and replayed: no thinking blocks, no tool calls or tool
  results. Earlier turns are therefore plain text, so sliding the window never
  edits anything the model is strict about.
- The window must start with a `user` message: drop a leading `assistant`. If a turn
  failed, two `user` messages can sit side by side — merge consecutive same-role
  messages when building the window.
- The user message is saved before calling Claude, the reply after it succeeds; a
  failed turn leaves the user message on record with no reply.
- Turns for one chat run one at a time (a per-chat lock), so history order is the
  order things happened.

**Telegram.** `channels/telegram.py` polls (message updates only) and hands text from
allowed chats to `Agent.reply()`. Strangers are logged and get nothing, not even
"typing…". `/start` gets a fixed greeting; non-text messages get "text only for
now". Updates are handled one at a time. Messages sent while Shelly was down are
answered on restart. The agent is built in `post_init` by a `start()` coroutine
from `__main__`, because the async Mongo and Anthropic clients must be created
inside the loop `run_polling()` owns.

`channels/telegram_format.py` converts Claude's Markdown to the HTML subset
Telegram accepts (`b i s code pre a blockquote`), escaping everything else; code
and links are held aside so emphasis rules cannot reach into them, `**` is the
only bold (`__` is left alone — it is nearly always a dunder), and only http(s)
links become links. `split()` cuts at 3800 characters on paragraph, then line,
boundaries, and closes/reopens a code fence cut in half. Each piece is sent as
HTML and falls back to plain text if Telegram rejects it or it grows past 4096.

## Roadmap

v1 — chat over Telegram with short-term memory
- [x] Repo scaffold: package layout, `requirements.txt`, `.venv`, `README.md`
- [x] `config.py`: load and validate `.env`, fail fast on anything missing
- [x] `memory/history.py`: save, window (limit, leading-assistant drop, merge), index
- [x] `core/agent.py` + `core/tools.py`: the loop and an empty registry
- [x] `channels/telegram.py`: polling, allowlist, typing indicator, HTML + split
- [ ] System prompt: who Shelly is, who the operator is, reply style for a phone
  (a short placeholder is in `core/prompt.py`)
- [ ] Tests: allowlist, window rules, tool loop with a fake client, formatting
- [x] Run on the Jetson and survive a reboot (systemd service)

Later — candidates, not commitments
- [ ] Long-term memory capability: tools to save and recall durable facts
- [ ] First real capabilities (to be chosen) as modules in `capabilities/`
- [ ] Commands: `/reset` (start a fresh window), `/status`
- [ ] Dashboard: conversation view and usage in `../Shelly-Web-App`
- [ ] Usage/cost logging per turn (`response.usage`)
- [ ] Approval prompts in Telegram before tools with side effects run
- [ ] Rotate `logs/shelly.log` (it is appended to forever)
