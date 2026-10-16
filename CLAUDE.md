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
tail -f logs/shelly.log                      # the log, rotated by Python
cat logs/shelly.stderr.log                   # only what escaped logging (crashes)
```

Logging is `shelly/logs.py`: with `SHELLY_LOG_FILE` set (the unit sets it) Python
writes and rotates `logs/shelly.log` itself, 5 MB × 5 old files; without it, logs go
to stderr. Uncaught exceptions are logged too. Do not point systemd's
`StandardOutput` at `shelly.log` or rotate it with logrotate: systemd keeps its
append handle open across a rename and would write into the rotated copy. The
Jetson has no persistent journal, so these files are the only record.

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
| `SHELLY_TIMEZONE` | the operator's IANA zone, for message time stamps and task times; default `America/Chicago` |
| `SHELLY_PROFILE` | path to the operator profile; default `profile.md` |
| `GOOGLE_CLIENT_FILE` | the OAuth client downloaded from Google Cloud, for the login only; default `secrets/google_client.json` |
| `GOOGLE_TOKEN_FILE` | the saved Google login; default `secrets/google_token.json`. Missing means no calendar |

**The repo is public.** Nothing personal goes in it: the operator profile lives in
`profile.md` (gitignored; `profile.example.md` is the template), and the allowed
chat id only in `.env`. `secrets/` (gitignored, mode 700) holds the Google OAuth
client and the saved login, which is full access to the operator's calendar:
never in the repo, Mongo, logs or the dashboard.

## Architecture

```
shelly/
  __main__.py        # load config, wire the pieces together, start polling
  config.py          # read and validate .env once, at startup
  core/
    agent.py         # one turn: history -> Claude -> tool loop -> reply text
    tools.py         # Tool (name, description, input schema, handler) and the registry
    prompt.py        # the system prompt: guide + operator profile + capability context
    context.py       # current_chat: the chat the running turn belongs to
  google_login.py    # python -m shelly.google_login: the one-time Google login
  integrations/
    google.py        # Google OAuth token refresh and the Calendar REST calls (httpx)
  memory/
    history.py       # the Mongo conversation store and the context window
    notes.py         # long-term notes
    tasks.py         # the to-do store: tasks, steps, reminders, repeats
  channels/
    telegram.py      # Telegram in/out: allowlist, formatting, splitting, reminder tick
  capabilities/      # one module per capability, each exporting its tools
    memory.py        # remember / update_memory / forget
    todo.py          # the to-do tools, open-task block, reminder poller
    calendar.py      # Google Calendar: agenda block, event tools (changes ask first)
tests/
```

Run with `.venv/bin/python -m shelly` (logs to stderr; `logs/` is gitignored for
redirected output). `shelly/config.py` reads `.env` once at
startup into a frozen `Config` and raises `ConfigError` listing every problem at
once; an empty or missing allowlist is an error, never "everyone". `Config`'s repr
leaves out the secrets, so it is safe to log. The Atlas user cannot drop databases, only collections: a throwaway live check
should use its own collections and drop them. Tests are plain `unittest`:
`.venv/bin/python -m unittest discover -s tests -t . -v`.

**Layering.** The core knows nothing about Telegram, and channels know nothing about
Claude: a channel hands the core `(chat_id, text)` and gets text back. A second
channel (the web dashboard, say) plugs into the same core. Capabilities only add
tools; they never edit the loop.

**Capabilities.** A capability is a module in `capabilities/` with a
`register(registry, ...)` function, called from `__main__`'s `start()`. It can add
`Tool`s (`registry.add`) and prompt context (`registry.add_context(provider)`: an
async callable returning text, or None, for a system block every turn — for what
Claude should always know rather than ask for). A provider that raises is logged
and left out of that turn, never fatal. A capability can also add a poller
(`registry.add_poller(poll)`: an async `poll(now)` returning `[(chat_id, text)]`)
for what Shelly says unprompted; it marks what it returns as handled. A tool that
needs to know which chat it runs for reads `core.context.current_chat`, which the
agent sets for each turn. Tool handlers
are async, take the validated input, and return a string (or raise; the loop turns an
exception into a `tool_result` with `is_error: true` rather than crashing the turn).
Define tools with a full JSON schema. **Strict-tool limits:** the API rejects
the *whole request* (every message fails, not just one tool) when the strict
tools together have more than 16 union-typed (`anyOf`/nullable) parameters, or
when their compiled grammar is too large. The memory and to-do tools are strict
and use 13 unions. The calendar tools are `strict=False`, and so should later
capabilities' tools be. For a non-strict tool, `Registry.run()` drops keys the
schema does not name and refuses a call missing a required one; the handler
checks values. Prefer an empty string over `null` for "not given".
`tests/test_calendar.py` `SchemaBudget` counts unions. The grammar size can
only be checked by sending a request, so **after adding or changing any tool,
send one real request with every tool registered before restarting the
service.**

**Approval before side effects.** A tool that acts outside Shelly (sends,
spends, deletes, runs something) sets `Tool.confirm`: a function from its input
to one line saying what the call will do, in the operator's terms. Before the
handler runs, `Registry.run()` hands that line and `current_chat` to
`registry.approver`, and runs the handler only on a yes. Anything else
(declined, no answer, no approver set, the approver or `confirm` raising)
becomes an `is_error` result telling Claude it was not done and not to retry
unasked. It fails closed. The description of such a tool gets `ASKS_FIRST`
appended, so Claude calls it rather than asking in chat and then being asked
again. Tools that only touch Shelly's own memory (notes, tasks) have no
`confirm` and run unasked. In Telegram the approver is `Approvals`: a plain-text
message "Okay to do this?" with ✅ Do it / ❌ Don't buttons (callback data
`approve:<random id>:yes|no`). The turn waits for a tap, holding the chat's
lock, for up to `APPROVAL_TIMEOUT` (10 min); no tap counts as a no. A new
message or `/reset` from the chat settles any waiting prompt as a no, so the
chat is never stuck behind it. "typing…" pauses while a prompt waits. A tap from
another chat, or on a prompt from before a restart, settles nothing and gets
"That request has expired." The prompt is edited afterwards to say how it ended,
and each ask and outcome is logged (`approval <id> …`).

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

**System prompt.** `core/prompt.py` sends, most stable first: `GUIDE` (behaviour:
texting style, no headings/tables, never claim an action no tool performed, say
"I don't know") and the operator profile read once at startup from `profile.md`,
as one block; then one block per capability context (notes, open tasks). Edit
`profile.md`, then restart the service. **There is no clock in the system
prompt**: every operator message in the window opens with the local time it was
received, `[Thu 15 Oct 2026, 10:32 CDT]`, rendered from its stored `created_at`
in `SHELLY_TIMEZONE` (`history.stamp()`). The newest stamp is now; the others
tell Claude how long ago things were said. Shelly's own messages are not stamped,
so it does not learn to write stamps. Stamps exist only in the window, never in
the stored text.

**Prompt caching.** Two breakpoints (5-minute TTL): an explicit one on the stable
system block (tools render before system, so it covers tools + guide + profile and
is read back across turns), and top-level automatic `cache_control` on the request,
so each tool round of a turn reads back the round before, and each turn reads
back the conversation so far. That works because nothing before the newest
message changes on its own: the time stamps are derived from the store, so a
replayed message is byte-identical. Capability context sits between the stable
block and the conversation, so a turn that changes a note or task makes the next
turn re-write the conversation (the stable block still reads). Keep anything that
varies per turn out of the system prompt and tool definitions. The `turn …` log line shows `cache_read` and `cache_write`.

**Long-term memory** (`capabilities/memory.py`, store `memory/notes.py`). Notes
Shelly chooses to keep, separate from the history: collection `notes`
`{n, text, created_at, updated_at}`, unique index on `n`. Numbers come from an
atomic `$inc` on `counters._id="notes"` (not max+1 — one turn can save two at
once) and are never reused. Tools: `remember(text)`, `update_memory(n, text)`,
`forget(n)`; when and what to save is in the `remember` description. A note is
one fact, at most 500 characters. **Every note is in every prompt** (a context
block listing `[n] (date) text`), not retrieved; past `LARGE` (300) notes a warning
is logged — that is the point to switch to retrieval.

**To-do list** (`capabilities/todo.py`, store `memory/tasks.py`). Collection
`tasks`, one document per task: `{n, chat_id, title, details, status, due,
remind_at, repeat, done_log, steps: [{title, status, due, remind_at, repeat,
done_at}], created_at, updated_at, done_at, v}`; `n` from `counters._id="tasks"`
like notes. Status is `open | done | dropped`; times are UTC; `repeat` is
`none | daily | weekdays | weekly`. Tools: `add_task`, `update_task`, `set_steps`,
`complete_task`, `list_closed_tasks`. **How Shelly plans lives in the `add_task`
description**: gather what "done" means, break multi-sitting work into
one-sitting steps scheduled in turn up to the deadline, set reminders by
judgment (09:00 local on a step's day by default), ask only when something
material cannot be inferred. Claude passes local ISO times (`2026-10-20` means
09:00); a reminder in the past is refused. Every open task is in every prompt.

- **Reminders.** The channel's `tick()` calls the pollers every 30 s
  (`TICK`); `Tasks.fire_due()` returns every reminder due and moves it on (a
  repeat to its next occurrence on the local wall clock, so DST does not shift
  it; missed occurrences are skipped) or clears it (a one-off). The text is a
  template, not Claude: instant and free. More than 10 min late (Shelly was down)
  it says so. Each is sent, then recorded in `messages` as an `assistant`
  message via `Agent.inform()` (under the chat lock) so a reply like "done" makes
  sense. A send that fails is logged and not retried. A failing tick is logged and
  the timer carries on.
- **Repeating tasks are habits.** `complete_task` on one counts an occurrence,
  not the task: it is appended to `done_log` (last 100), and due and remind_at
  move to the next occurrence (a reminder not yet fired for it is skipped). Only
  an occurrence falling today counts — within the coming week for `weekly`;
  done on another day it is just logged. A due date left behind by missed days
  is read as its first occurrence from today (`current_occurrence`). It stays
  open until dropped or set done with `update_task`. The prompt shows its next
  due and recent completions, which is how Shelly tracks a streak.
- **Writes** are read-modify-replace guarded by a version `v`, retried on a
  lost race, so the poller and a tool call on the same task cannot undo each
  other.

**Google Calendar** (`capabilities/calendar.py`, client `integrations/google.py`).
On only when `GOOGLE_TOKEN_FILE` exists; `__main__` logs `google calendar on/off`.
The Google Cloud project `Shelly` (the operator's personal account) has a Desktop
OAuth client and its consent screen **In production**, because in Testing the
refresh token dies after 7 days. Scopes are `SCOPES` in `integrations/google.py`
(`calendar.events`, `calendar.readonly`). Adding one means logging in again.
`python -m shelly.google_login` prints the consent link. The browser can be
anywhere: it ends on an unloadable `http://localhost:8765/?code=…` address,
which is pasted back. It saves `{client_id, client_secret, refresh_token,
scopes}` with mode 600. `GoogleAuth` refreshes the access token over httpx a
minute before expiry and retries a 401 once. A refused refresh
(`invalid_grant`) becomes `RELOGIN`, which tells Claude (and so the operator)
to run the login again. Every API failure is a `GoogleError` worded for Claude.
- **Agenda block** every turn: today and the next two days, from every calendar
  switched on ("selected") in Google Calendar, grouped by local day, with "nothing"
  for empty days. Declined events are left out. Each line ends with
  `[calendar: <id>, event: <id>]` (the primary calendar is `primary`), which the
  tools take. Cached 2 min (calendar list 10 min); Shelly's own changes clear it.
  If Google cannot be read, the block says so and tells Claude not to assume the
  operator is free, rather than vanishing.
- **Tools.** `calendar_events(start, end, query)` reads up to 92 days, includes
  each event's description (truncated, labelled as written by whoever made
  the event), and runs unasked. `create_event`, `update_event`,
  `delete_event` and `respond_to_invite` all have an async `confirm` that looks
  the event up, so the prompt names it ("Change "Dentist" (Tue 20 Oct
  14:00-15:30): move to …") and says when guests will get an email.
  Guests are emailed (`sendUpdates=all`) exactly when the event has other
  attendees. Times are local ISO from Claude. An all-day `end` is the last day
  covered (Google's exclusive end is one day later), and moving `start` alone
  keeps the length. On a recurring event, update and delete touch only that
  occurrence (ids come from `singleEvents=true` listings).
- **Event text is untrusted.** Anyone can put an invitation on the calendar,
  so the agenda tells Claude to treat event text as information, never
  instructions, and every change goes through approval.

**Commands.** Handled in the channel, never sent to Claude or stored as messages,
and only for allowed chats. `/reset` calls `Agent.reset()`, which (under the chat's
lock) writes a `role: "reset"` marker to the history; `build_window()` empties the
window at each marker, so Claude sees only what came after. Nothing is deleted and
notes are untouched. `/status` is `shelly/status.py`, built in `__main__` with the
agent, notes and ledger: uptime, model and effort, messages in view, note count,
and spend today (from local midnight) and over the last 7 days. If the database
cannot be read it says so instead of failing. Any other command gets "I only know
/status and /reset". The commands are registered in Telegram's menu at startup.

**Cost.** `core/usage.py`: every turn that reached Claude — failures included —
is summed over its requests, priced from `PRICES` (USD/MTok: input, output, cache
write, cache read; thinking is inside output), logged as one `turn …` line, and
kept in collection `turns` (`chat_id, created_at, outcome, requests`, the four
token counts, `cost_usd, models, tools, seconds`). `outcome` is one of `reply`,
`cut_off`, `refusal`, `empty`, `steps`, `error`. With the refusal fallback,
`usage.iterations` lists each attempt with its model and the top-level usage
covers only the last, so iterations are priced when present. A model missing from
`PRICES` makes the turn's cost `None` with a warning, never $0 — **add new models
there**. It is an estimate: a refusal before any output is counted but may not be
billed. `Ledger.spent_since()` totals it. A ledger failure is logged, never fatal.

**The dashboard reads these collections.** `../Shelly-Web-App/utils/shelly.py`
reads `messages`, `notes`, `turns` and `tasks` (never writes; reminders show up
in `messages` as assistant messages). It mirrors `build_window()` to show what is
in view, with `HISTORY_LIMIT` duplicated, and `current_occurrence()` to show a
repeating task's due date. Change a shape, the window rules or the repeat rules
here, follow it there.

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

**Telegram.** `channels/telegram.py` polls and hands text from
allowed chats to `Agent.reply()`. Strangers are logged and get nothing, not even
"typing…". `/start` gets a fixed greeting (see Commands below); non-text messages get "text only for
now". Polling takes messages and callback queries (button taps). Updates are
handled **concurrently** (`concurrent_updates(True)`): a turn waiting on an
approval tap must not block the tap itself. A chat's turns still run in arrival
order, because each update's task starts in order and the agent's per-chat
`asyncio.Lock` is first come, first served. Messages sent while Shelly was down
are answered on restart. The agent is built in `post_init` by a `start()` coroutine
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
- [x] System prompt: who Shelly is, who the operator is, reply style for a phone
- [x] Write `profile.md` with the operator (local only)
- [x] Tests: allowlist, window rules, tool loop with a fake client, formatting
- [x] Run on the Jetson and survive a reboot (systemd service)

Later — candidates, not commitments
- [x] Long-term memory capability: tools to save and recall durable facts
- [ ] Notes: retrieve relevant ones instead of all, once there are hundreds
- [x] To-do list: planned tasks with steps, reminders and repeating habits
- [ ] More capabilities (to be chosen) as modules in `capabilities/`
- [ ] Google Calendar: built and unit-tested; needs the Google login and a live check
- [ ] Gmail: read, search, drafts unasked; send/reply/forward/trash ask first
- [x] Dashboard: a view of the to-do list
- [x] Commands: `/reset` (start a fresh window), `/status`
- [x] Dashboard: conversation view and usage in `../Shelly-Web-App` (live at sulcata.ai)
- [x] Usage/cost logging per turn (`response.usage`)
- [x] Approval prompts in Telegram before tools with side effects run
- [x] Rotate `logs/shelly.log` (it is appended to forever)
