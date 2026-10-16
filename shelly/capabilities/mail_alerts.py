''' new-mail alerts: Shelly messages the operator when important mail reaches the inbox

Every CHECK_EVERY seconds the poller reads Gmail's history since the history id
it last saw (kept in Mongo, so a restart neither repeats nor skips mail). A new
message is worth telling about when it is in the inbox, unread, not sent by the
operator, and either matches an "always" rule or is what Gmail itself calls
important in the Primary tab -- unless a "never" rule matches. No Claude call:
the alert is a template, instant and free, with sender and subject taken from
the headers.

Between QUIET_FROM and QUIET_UNTIL local time nothing is sent: alerts are held
and go out as one overnight summary at the first check after QUIET_UNTIL,
leaving out whatever was read or moved in the meantime.

Each alert is recorded in the conversation with the thread ids (the record),
so "reply to Ana" next turn can go straight to read_thread. The text sent is
the same without them.
'''

import asyncio
import logging
import re
import time
from datetime import datetime
from email.utils import parseaddr

from shelly.core.tools import Tool
from shelly.integrations.google import GoogleError
from shelly.memory.mail_watch import KINDS


log = logging.getLogger(__name__)

CHECK_EVERY = 300
QUIET_FROM, QUIET_UNTIL = 22, 7   # local hours
SHOWN = 10                        # alerts listed in one message; the rest are counted
HELD = 50                         # at most this many waiting through the night

RULE = '''Change which new mail Shelly alerts the operator about. Shelly checks the inbox \
every few minutes and messages them about new unread mail that Gmail marks important in \
the Primary tab, except between 22:00 and 07:00, when it holds alerts for one morning \
summary. Rules override that: "always" alerts about matching mail even if Gmail does not \
think it important; "never" silences it. match is an email address (ana@example.com), a \
domain starting with @ (@example.com), or else a word or phrase looked for in the \
subject. action add or remove. The current rules are in your prompt when there are any.'''


def _object(**properties):
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


def quiet(moment, tz):
    hour = moment.astimezone(tz).hour

    return hour >= QUIET_FROM or hour < QUIET_UNTIL


def plain(text, limit):
    ''' header text safe to show: no Markdown that would turn a subject into a link or code '''

    text = re.sub(r'[\[\]`*_~<>]', '', ' '.join((text or '').split()))

    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def matches(rule, address, subject):
    rule = rule.lower()

    if rule.startswith('@'):
        return address.endswith(rule)

    if '@' in rule:
        return address == rule

    return rule in subject.lower()


def worth_telling(meta, rules):
    ''' should this newly added message raise an alert '''

    labels = set(meta.get('labelIds', []))

    if 'INBOX' not in labels or 'UNREAD' not in labels or 'SENT' in labels:
        return False

    head = {h['name'].lower(): h['value'] for h in meta.get('payload', {}).get('headers', [])}
    address = parseaddr(head.get('from', ''))[1].lower()
    subject = head.get('subject', '')

    if any(matches(r, address, subject) for r in rules['never']):
        return False

    if any(matches(r, address, subject) for r in rules['always']):
        return True

    # Primary is CATEGORY_PERSONAL, or no category at all when tabs are off.
    other_tab = any(l.startswith('CATEGORY_') and l != 'CATEGORY_PERSONAL' for l in labels)

    return 'IMPORTANT' in labels and not other_tab


def item(meta):
    head = {h['name'].lower(): h['value'] for h in meta.get('payload', {}).get('headers', [])}
    name, address = parseaddr(head.get('from', ''))

    return {'id': meta['id'], 'thread_id': meta.get('threadId'),
            'sender': plain(name or address or '?', 60),
            'subject': plain(head.get('subject') or '(no subject)', 120)}


def compose(items, overnight):
    ''' (text to send, record for the history) '''

    title = f'📬 Overnight mail ({len(items)}):' if overnight else '📬 New mail:'
    shown = items[:SHOWN]
    lines = [f'- {i["sender"]}: "{i["subject"]}"' for i in shown]
    more = [f'- and {len(items) - SHOWN} more'] if len(items) > SHOWN else []
    record = [f'{line} [thread: {i["thread_id"]}]' for line, i in zip(lines, shown)]

    return '\n'.join([title, *lines, *more]), '\n'.join([title, *record, *more])


def register(registry, api, store, tz, chat_id, clock=time.monotonic):
    ''' the rule tool and its prompt block, and -- with a chat to tell -- the poller '''

    async def mail_alert_rule(action, kind, match):
        match = (match or '').strip().lower()

        if kind not in KINDS or action not in ('add', 'remove') or not match:
            raise GoogleError('action must be add or remove, kind always or never, and match not empty.')

        if action == 'add':
            await store.add_rule(kind, match)
            return f'Added: {kind} alert for {match}.'

        if await store.remove_rule(kind, match):
            return f'Removed the {kind} rule for {match}.'

        return f'There was no {kind} rule for {match}.'

    async def rules_block():
        rules = await store.rules()

        if not any(rules.values()):
            return None

        return ('Mail alert rules (see mail_alert_rule): '
                + '; '.join(f'{kind}: {", ".join(rules[kind])}' for kind in KINDS if rules[kind]))

    registry.add(Tool('mail_alert_rule', RULE,
                      _object(action={'type': 'string', 'enum': ['add', 'remove']},
                              kind={'type': 'string', 'enum': list(KINDS)},
                              match={'type': 'string'}),
                      mail_alert_rule, strict=False))
    registry.add_context(rules_block)

    if chat_id is None:
        log.warning('mail alerts off: they need exactly one allowed chat to go to')
        return

    due = [0.0]

    async def metas(ids):
        ''' message metadata for each id; one deleted since (404) is just left out '''

        async def one(message_id):
            try:
                return await api.message(message_id)
            except GoogleError as error:
                if error.status == 404:
                    return None
                raise

        return [m for m in await asyncio.gather(*(one(i) for i in ids)) if m]

    async def poll(now):
        if clock() < due[0]:
            return []

        due[0] = clock() + CHECK_EVERY
        state = await store.state()

        if not state.get('history_id'):
            # First run: start from now rather than announcing the whole inbox.
            state['history_id'] = (await api.profile())['historyId']
            await store.save_state(state)
            log.info('mail alerts start at history %s', state['history_id'])
            return []

        try:
            added, latest = await api.history(state['history_id'])
        except GoogleError as error:
            if error.status != 404:
                raise

            state['history_id'] = (await api.profile())['historyId']
            await store.save_state(state)
            log.warning('mail history too old; alerts restart from now (missed mail is not announced)')
            return []

        rules = await store.rules()
        held = state.get('pending', [])
        seen = {i['id'] for i in held}
        new = [item(m) for m in await metas([i for i in added if i not in seen]) if worth_telling(m, rules)]
        state['history_id'] = latest

        if quiet(now, tz):
            state['pending'] = (held + new)[-HELD:]
            await store.save_state(state)

            if new:
                log.info('mail alerts: holding %d for the morning', len(new))

            return []

        if held:
            # Read or moved overnight: no need to hear about it now.
            still = {m['id'] for m in await metas([i['id'] for i in held]) if worth_telling(m, rules)}
            held = [i for i in held if i['id'] in still]

        state['pending'] = []
        await store.save_state(state)
        items = held + new

        if not items:
            return []

        log.info('mail alerts: %d new, %d from overnight', len(new), len(held))
        text, record = compose(items, overnight=bool(held))

        return [(chat_id, text, record)]

    registry.add_poller(poll)
