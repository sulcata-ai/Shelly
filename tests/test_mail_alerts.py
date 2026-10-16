''' new-mail alerts: what raises one, when it is sent, and what the history keeps

No network: Gmail's history and messages are scripted. The traps: the whole
inbox announced on first run, promotions or read mail announced, a 2 a.m.
ping, mail read overnight announced in the morning anyway, a too-old history
id wedging the check, and a subject smuggling a link into the alert.
'''

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from shelly.capabilities import mail_alerts as alerts
from shelly.channels import telegram as channel_module
from shelly.core.tools import Registry
from shelly.integrations.google import GoogleError
from shelly.memory.mail_watch import MailWatch
from tests.fakes import FakeDb

TZ = ZoneInfo('America/Chicago')
CHAT = 1111
NOON = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)      # 12:00 local
NIGHT = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)      # 02:00 local
MORNING = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)   # 08:00 local


def run(coroutine):
    return asyncio.run(coroutine)


def meta(id, sender='Ana <ana@example.com>', subject='Lunch?', labels=('INBOX', 'UNREAD', 'IMPORTANT', 'CATEGORY_PERSONAL')):
    return {'id': id, 'threadId': f't-{id}', 'labelIds': list(labels),
            'payload': {'headers': [{'name': 'From', 'value': sender}, {'name': 'Subject', 'value': subject}]}}


class FakeGmail:

    def __init__(self):
        self.messages = {}
        self.added = []          # ids the next history call returns
        self.history_id = '100'
        self.too_old = False

    async def profile(self):
        return {'historyId': self.history_id}

    async def history(self, start):
        if self.too_old:
            raise GoogleError('Google said 404: Requested entity was not found.', 404)
        added, self.added = self.added, []
        return added, str(int(start) + 1)

    async def message(self, message_id):
        if message_id not in self.messages:
            raise GoogleError('Google said 404: Not Found', 404)
        return self.messages[message_id]

    def arrive(self, *metas):
        for m in metas:
            self.messages[m['id']] = m
            self.added.append(m['id'])


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Setup(unittest.TestCase):

    def setUp(self, chat=CHAT):
        self.gmail, self.clock = FakeGmail(), Clock()
        self.store = MailWatch(FakeDb())
        self.registry = Registry()
        alerts.register(self.registry, self.gmail, self.store, TZ, chat, clock=self.clock)
        run(self.check(NOON))  # first run: just sets the starting point

    async def check(self, now):
        self.clock.now += alerts.CHECK_EVERY
        return await self.registry.poll(now)

    def poll(self, now=NOON):
        return run(self.check(now))


class FirstRun(Setup):

    def test_it_starts_from_now_and_says_nothing(self):
        state = run(self.store.state())
        self.assertEqual(state['history_id'], '100')

    def test_it_waits_between_checks(self):
        self.gmail.arrive(meta('m1'))
        self.assertEqual(run(self.registry.poll(NOON)), [], 'checked a moment ago')
        self.assertEqual(len(self.poll()), 1)


class WhatAlerts(Setup):

    def test_important_primary_unread_mail_alerts_with_ids_only_in_the_record(self):
        self.gmail.arrive(meta('m1'))
        [(chat, text, record)] = self.poll()

        self.assertEqual(chat, CHAT)
        self.assertEqual(text, '📬 New mail:\n- Ana: "Lunch?"')
        self.assertEqual(record, '📬 New mail:\n- Ana: "Lunch?" [thread: t-m1]')

    def test_promotions_unimportant_read_and_sent_mail_stay_quiet(self):
        self.gmail.arrive(
            meta('p', labels=('INBOX', 'UNREAD', 'IMPORTANT', 'CATEGORY_PROMOTIONS')),
            meta('u', labels=('INBOX', 'UNREAD', 'CATEGORY_PERSONAL')),
            meta('r', labels=('INBOX', 'IMPORTANT', 'CATEGORY_PERSONAL')),
            meta('s', labels=('INBOX', 'UNREAD', 'IMPORTANT', 'SENT')),
        )
        self.assertEqual(self.poll(), [])

    def test_no_tabs_counts_as_primary(self):
        self.gmail.arrive(meta('m1', labels=('INBOX', 'UNREAD', 'IMPORTANT')))
        self.assertEqual(len(self.poll()), 1)

    def test_rules_override_gmail(self):
        tool = self.registry._tools['mail_alert_rule'].handler
        run(tool(action='add', kind='always', match='@school.example'))
        run(tool(action='add', kind='always', match='invoice'))
        run(tool(action='add', kind='never', match='ana@example.com'))
        plain = ('INBOX', 'UNREAD', 'CATEGORY_UPDATES')

        self.gmail.arrive(meta('a', sender='Office <office@school.example>', labels=plain),
                          meta('b', sender='bo@shop.example', subject='Your Invoice #4', labels=plain),
                          meta('c'))  # important, but Ana is on never
        [(_, text, _)] = self.poll()

        self.assertIn('Office', text)
        self.assertIn('Your Invoice #4', text)
        self.assertNotIn('Ana', text)

    def test_a_subject_cannot_become_a_link(self):
        self.gmail.arrive(meta('m1', subject='[Click here](https://evil.example) `now`'))
        [(_, text, _)] = self.poll()

        self.assertNotIn('](', text)
        self.assertNotIn('`', text)

    def test_a_flood_is_counted_not_listed(self):
        self.gmail.arrive(*(meta(f'm{i}', subject=f'note {i}') for i in range(13)))
        [(_, text, _)] = self.poll()

        self.assertEqual(text.count('\n- '), alerts.SHOWN + 1)
        self.assertTrue(text.endswith('- and 3 more'))

    def test_a_message_deleted_before_the_check_is_skipped(self):
        self.gmail.added.append('gone')
        self.assertEqual(self.poll(), [])


class QuietHours(Setup):

    def test_night_mail_waits_for_the_morning_and_read_mail_is_dropped(self):
        self.gmail.arrive(meta('m1', subject='One'), meta('m2', subject='Two'))
        self.assertEqual(self.poll(NIGHT), [])

        self.gmail.messages['m2']['labelIds'].remove('UNREAD')  # read on the phone at 6
        self.gmail.arrive(meta('m3', subject='Three'))
        [(_, text, _)] = self.poll(MORNING)

        self.assertEqual(text, '📬 Overnight mail (2):\n- Ana: "One"\n- Ana: "Three"')
        self.assertEqual(run(self.store.state())['pending'], [])

    def test_quiet_runs_from_22_to_7(self):
        at = lambda h: datetime(2026, 10, 16, h, 0, tzinfo=TZ)
        self.assertEqual([alerts.quiet(at(h), TZ) for h in (6, 7, 21, 22, 23)], [True, False, False, True, True])


class Recovery(Setup):

    def test_a_too_old_history_id_restarts_from_now(self):
        self.gmail.too_old, self.gmail.history_id = True, '900'

        with self.assertLogs(alerts.log, level='WARNING'):
            self.assertEqual(self.poll(), [])

        self.assertEqual(run(self.store.state())['history_id'], '900')

    def test_the_position_survives_a_restart(self):
        self.gmail.arrive(meta('m1'))
        self.poll()

        registry = Registry()  # a fresh process, same database
        alerts.register(registry, self.gmail, self.store, TZ, CHAT, clock=self.clock)
        self.clock.now += alerts.CHECK_EVERY
        self.assertEqual(run(registry.poll(NOON)), [], 'm1 is not announced again')


class RulesTool(Setup):

    def test_rules_show_in_the_prompt_only_when_there_are_some(self):
        tool = self.registry._tools['mail_alert_rule'].handler
        self.assertEqual(run(self.registry.context()), [])

        run(tool(action='add', kind='always', match='Ana@Example.com'))
        self.assertEqual(run(self.registry.context()), ['Mail alert rules (see mail_alert_rule): always: ana@example.com'])

        self.assertIn('Removed', run(tool(action='remove', kind='always', match='ana@example.com')))
        self.assertIn('no always rule', run(tool(action='remove', kind='always', match='ana@example.com')))

    def test_the_tool_runs_unasked_and_is_not_strict(self):
        tool = self.registry._tools['mail_alert_rule']
        self.assertIsNone(tool.confirm)
        self.assertFalse(tool.strict)


class NoChat(unittest.TestCase):

    def test_without_one_chat_there_is_no_poller_but_rules_still_work(self):
        registry = Registry()

        with self.assertLogs(alerts.log, level='WARNING'):
            alerts.register(registry, FakeGmail(), MailWatch(FakeDb()), TZ, None)

        self.assertEqual(registry._pollers, [])
        self.assertIn('mail_alert_rule', registry._tools)


class TheChannelStoresTheRecord(unittest.TestCase):

    def test_text_is_sent_and_record_is_kept(self):
        sent, kept = [], []

        async def poll(now):
            return [(CHAT, 'shown', 'shown [thread: t1]')]

        async def inform(chat_id, text):
            kept.append(text)

        async def send_message(chat_id, text, parse_mode=None):
            sent.append(text)

        registry = Registry()
        registry.add_poller(poll)
        agent = SimpleNamespace(registry=registry, inform=inform)
        run(channel_module.tick_once(SimpleNamespace(send_message=send_message), agent, {CHAT}))

        self.assertEqual((sent, kept), (['shown'], ['shown [thread: t1]']))


if __name__ == '__main__':
    unittest.main()
