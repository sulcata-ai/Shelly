''' the to-do list: the store, the tools Claude drives it with, and reminders

The traps: a date read in UTC instead of the operator's zone, a reminder that
fires twice or never stops, a repeat that drifts an hour across daylight saving,
a done step that comes back undone after its steps are revised, a habit that
ends the first time it is done or nags about a day already done, and a reminder
sent to a chat that is not allowed.
'''

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from shelly.capabilities import todo
from shelly.channels import telegram as channel
from shelly.core.context import current_chat
from shelly.core.tools import Registry
from shelly.memory.tasks import TaskError, Tasks, next_occurrence, parse_when
from tests.fakes import FakeDb

CHICAGO = ZoneInfo('America/Chicago')
UTC = timezone.utc

# Wednesday 14 October 2026, 16:00 Chicago time
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)


def run(coroutine):
    return asyncio.run(coroutine)


def step(title, due=None, remind_at=None, repeat='none', status=None):
    s = {'title': title, 'due': due, 'remind_at': remind_at, 'repeat': repeat}
    if status:
        s['status'] = status
    return s


class Times(unittest.TestCase):

    def test_a_bare_date_is_nine_in_the_morning_local(self):
        self.assertEqual(parse_when('2026-10-20', CHICAGO, 'due'), datetime(2026, 10, 20, 14, 0, tzinfo=UTC))

    def test_a_naive_datetime_is_local_and_an_offset_is_respected(self):
        self.assertEqual(parse_when('2026-10-20T18:30', CHICAGO, 'x'), datetime(2026, 10, 20, 23, 30, tzinfo=UTC))
        self.assertEqual(parse_when('2026-10-20T18:30+00:00', CHICAGO, 'x'), datetime(2026, 10, 20, 18, 30, tzinfo=UTC))

    def test_nonsense_is_an_error_claude_can_read(self):
        with self.assertRaisesRegex(TaskError, 'ISO'):
            parse_when('next tuesday', CHICAGO, 'due')

    def test_daily_keeps_the_wall_clock_across_daylight_saving(self):
        # 09:00 CDT on Sat 31 Oct; DST ends Sun 1 Nov 2026
        at = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
        after = next_occurrence(at, 'daily', CHICAGO, at)

        self.assertEqual(after.astimezone(CHICAGO).hour, 9)
        self.assertEqual(after, datetime(2026, 11, 1, 15, 0, tzinfo=UTC))

    def test_weekdays_skip_the_weekend_and_missed_ones_are_skipped(self):
        friday = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
        self.assertEqual(next_occurrence(friday, 'weekdays', CHICAGO, friday).astimezone(CHICAGO).weekday(), 0)

        week_later = friday + timedelta(days=7, hours=1)
        self.assertGreater(next_occurrence(friday, 'daily', CHICAGO, week_later), week_later)

    def test_one_offs_do_not_repeat(self):
        self.assertIsNone(next_occurrence(NOW, 'none', CHICAGO, NOW))


class Store(unittest.TestCase):

    def setUp(self):
        self.tasks = Tasks(FakeDb(), CHICAGO)

    def add(self, **kw):
        args = dict(chat_id=1, title='Taxes', details='', due=None, remind_at=None, repeat='none', steps=[])
        args.update(kw)
        return run(self.tasks.add(now=NOW, **args))

    def test_tasks_are_numbered_and_listed(self):
        self.assertEqual(self.add()['n'], 1)
        self.assertEqual(self.add(title='Gym')['n'], 2)
        self.assertEqual([t['title'] for t in run(self.tasks.open())], ['Taxes', 'Gym'])

    def test_a_reminder_in_the_past_is_refused(self):
        with self.assertRaisesRegex(TaskError, 'already past'):
            self.add(remind_at='2026-10-14T10:00')

    def test_finishing_the_last_step_finishes_the_task(self):
        self.add(steps=[step('a'), step('b')])
        self.assertEqual(run(self.tasks.complete(1, 1))['status'], 'open')
        self.assertEqual(run(self.tasks.complete(1, 2))['status'], 'done')
        self.assertEqual(run(self.tasks.open()), [])

    def test_revising_steps_keeps_done_ones_done(self):
        self.add(steps=[step('gather'), step('file')])
        first = run(self.tasks.complete(1, 1))['steps'][0]['done_at']

        revised = run(self.tasks.set_steps(1, [step('gather', status='done'), step('review'), step('file')]))

        self.assertEqual([s['status'] for s in revised['steps']], ['done', 'open', 'open'])
        self.assertEqual(revised['steps'][0]['done_at'], first)

    def test_update_leaves_null_fields_alone_and_clears_empty_ones(self):
        self.add(due='2026-10-31', details='keep me')
        task = run(self.tasks.update(1, title='Tax return', due=''))

        self.assertEqual((task['title'], task['details'], task['due']), ('Tax return', 'keep me', None))

    def test_a_missing_step_or_task_is_an_error(self):
        self.add(steps=[step('a')])

        with self.assertRaisesRegex(TaskError, 'no step 3'):
            run(self.tasks.complete(1, 3))

        with self.assertRaisesRegex(TaskError, 'no task 9'):
            run(self.tasks.complete(9))

    def test_a_stale_write_is_retried_not_lost(self):
        self.add()
        collection = self.tasks.tasks
        real = collection.replace_one
        calls = []

        async def racing(query, doc):
            if not calls:
                calls.append(1)
                # someone else saves first: our version is now stale
                collection.docs[0]['v'] += 1
                collection.docs[0]['details'] = 'theirs'
            return await real(query, doc)

        collection.replace_one = racing
        task = run(self.tasks.update(1, title='mine'))

        self.assertEqual((task['title'], task['details']), ('mine', 'theirs'))


class Reminders(Store):

    def test_a_one_off_fires_once(self):
        self.add(remind_at='2026-10-15T09:00')
        at = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)

        self.assertEqual(run(self.tasks.fire_due(at - timedelta(minutes=1))), [])
        self.assertEqual(len(run(self.tasks.fire_due(at))), 1)
        self.assertEqual(run(self.tasks.fire_due(at + timedelta(minutes=1))), [])

    def test_a_repeat_moves_on_and_stops_when_dropped(self):
        self.add(remind_at='2026-10-15T09:00', repeat='daily')
        at = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)

        [(task, _, _)] = run(self.tasks.fire_due(at))
        self.assertEqual(task['remind_at'], at + timedelta(days=1))

        run(self.tasks.update(1, status='dropped'))
        self.assertEqual(run(self.tasks.fire_due(at + timedelta(days=1))), [])


    def test_step_reminders_fire_for_their_step_and_not_once_done(self):
        self.add(steps=[step('a', remind_at='2026-10-15T09:00'), step('b', remind_at='2026-10-16T09:00')])
        run(self.tasks.complete(1, 2))

        fired = run(self.tasks.fire_due(datetime(2026, 10, 17, 14, 0, tzinfo=UTC)))

        self.assertEqual([(t['n'], i) for t, i, _ in fired], [(1, 0)])

    def test_a_long_overdue_reminder_is_flagged_late(self):
        self.add(remind_at='2026-10-15T09:00')
        [(_, _, late)] = run(self.tasks.fire_due(datetime(2026, 10, 15, 16, 0, tzinfo=UTC)))
        self.assertTrue(late)


class Habits(unittest.TestCase):
    ''' a weekday stretch at 07:30, reminded at 07:00, from Thu 15 Oct '''

    def setUp(self):
        self.tasks = Tasks(FakeDb(), CHICAGO)
        run(self.tasks.add(1, 'Stretch', '', '2026-10-15T07:30', '2026-10-15T07:00', 'weekdays', [], now=NOW))

    def at(self, day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=CHICAGO).astimezone(UTC)

    def test_done_after_the_reminder_counts_once_and_moves_on(self):
        run(self.tasks.fire_due(self.at(15, 7)))
        task = run(self.tasks.complete(1, now=self.at(15, 7, 20)))

        self.assertEqual(task['status'], 'open')
        self.assertEqual(task['done_log'], [self.at(15, 7, 20)])
        self.assertEqual(task['due'], self.at(16, 7, 30))
        self.assertEqual(task['remind_at'], self.at(16, 7))

    def test_done_before_the_reminder_skips_that_reminder(self):
        task = run(self.tasks.complete(1, now=self.at(15, 6)))

        self.assertEqual(task['remind_at'], self.at(16, 7))
        self.assertEqual(run(self.tasks.fire_due(self.at(15, 7, 1))), [])

    def test_friday_moves_to_monday(self):
        run(self.tasks.complete(1, now=self.at(16, 8)))  # Friday, after due, reminder unfired
        task = run(self.tasks.get(1))

        self.assertEqual(task['due'], self.at(19, 7, 30))
        self.assertEqual(task['remind_at'], self.at(19, 7))

    def test_missed_days_count_today_not_the_old_day(self):
        # reminders fired Thu and Fri (and moved on); not done until Tue
        run(self.tasks.fire_due(self.at(15, 7)))
        run(self.tasks.fire_due(self.at(16, 7)))
        task = run(self.tasks.complete(1, now=self.at(20, 12)))

        self.assertEqual(task['due'], self.at(21, 7, 30))
        self.assertEqual(task['remind_at'], self.at(21, 7))

    def test_done_before_the_first_day_is_logged_but_moves_nothing(self):
        task = run(self.tasks.complete(1, now=self.at(14, 17)))

        self.assertEqual(len(task['done_log']), 1)
        self.assertEqual((task['due'], task['remind_at']), (self.at(15, 7, 30), self.at(15, 7)))

    def test_a_weekly_chore_done_early_counts_for_its_week(self):
        run(self.tasks.update(1, repeat='weekly'))  # Thursdays
        task = run(self.tasks.complete(1, now=self.at(13, 12)))  # Tuesday before

        self.assertEqual(task['due'], self.at(22, 7, 30))
        self.assertEqual(task['remind_at'], self.at(22, 7))

    def test_without_a_due_date_todays_reminder_is_skipped(self):
        run(self.tasks.update(1, due=''))
        task = run(self.tasks.complete(1, now=self.at(15, 6)))

        self.assertEqual(task['remind_at'], self.at(16, 7))

    def test_update_to_done_still_ends_it(self):
        self.assertEqual(run(self.tasks.update(1, status='done'))['status'], 'done')


class Capability(unittest.TestCase):

    def setUp(self):
        self.tasks = Tasks(FakeDb(), CHICAGO)
        self.registry = Registry()
        todo.register(self.registry, self.tasks)
        current_chat.set(42)

    def call(self, name, **tool_input):
        text, is_error = run(self.registry.run(name, tool_input))
        self.assertFalse(is_error, text)
        return text

    def add(self, **kw):
        args = dict(title='File taxes', details='', due=None, remind_at=None, repeat='none', steps=[])
        args.update(kw)
        return self.call('add_task', **args)

    def test_the_tools_are_strict_with_every_field_required(self):
        def check(schema):
            if schema.get('type') == 'object':
                self.assertFalse(schema['additionalProperties'])
                self.assertEqual(set(schema['required']), set(schema['properties']))
                for sub in schema['properties'].values():
                    check(sub)
            for sub in schema.get('anyOf', []):
                check(sub)
            if 'items' in schema:
                check(schema['items'])

        params = self.registry.params()
        self.assertEqual({p['name'] for p in params},
                         {'add_task', 'update_task', 'set_steps', 'complete_task', 'list_closed_tasks'})

        for p in params:
            self.assertTrue(p['strict'])
            check(p['input_schema'])

    def test_a_task_remembers_the_chat_it_was_made_in(self):
        self.add()
        self.assertEqual(run(self.tasks.get(1))['chat_id'], 42)

    def test_mistakes_come_back_as_text(self):
        self.assertEqual(self.call('complete_task', n=5, step=None), 'There is no task 5.')
        self.assertIn('ISO', self.add(due='soon'))

    def test_the_open_list_is_in_the_prompt_in_local_time(self):
        self.add(due='2026-10-31T17:00', steps=[
            {'title': 'Gather W-2s', 'due': '2026-10-20', 'remind_at': '2026-10-20T09:00', 'repeat': 'none'}])

        [block] = run(self.registry.context())

        self.assertTrue(block.startswith(todo.HEADER))
        self.assertIn('#1 File taxes -- due Sat 31 Oct 17:00', block)
        self.assertIn('1. [ ] Gather W-2s -- due Tue 20 Oct 09:00 · reminder Tue 20 Oct 09:00', block)

    def test_a_habit_shows_its_next_due_and_recent_completions(self):
        self.add(title='Stretch', due='2026-10-01T07:30', repeat='daily')
        run(self.tasks.complete(1, now=datetime(2026, 10, 1, 13, 0, tzinfo=UTC)))

        [block] = run(self.registry.context())

        # the stored due is long past; the list shows the next one from today
        self.assertRegex(block, r'#1 Stretch -- next due \w{3} \d\d Oct 07:30')
        self.assertNotIn('02 Oct', block.splitlines()[1])
        self.assertIn('done 1 times; recently: Thu 01 Oct', block)

    def test_completing_a_habit_says_it_repeats(self):
        self.add(due='2026-12-01', repeat='weekly')
        self.assertTrue(self.call('complete_task', n=1, step=None).startswith('Done for this time'))

    def test_nothing_open_means_no_block(self):
        self.assertEqual(run(self.registry.context()), [])

    def test_a_due_step_produces_a_reminder_for_its_chat(self):
        self.add(steps=[{'title': 'Gather W-2s', 'due': None, 'remind_at': '2026-10-20T09:00', 'repeat': 'none'},
                        {'title': 'File', 'due': None, 'remind_at': None, 'repeat': 'none'}])

        [(chat_id, text)] = run(self.registry.poll(datetime(2026, 10, 20, 14, 0, tzinfo=UTC)))

        self.assertEqual(chat_id, 42)
        self.assertIn('**Gather W-2s** (step 1 of 2 of #1 File taxes', text)


class HabitReminder(unittest.TestCase):

    def test_a_missed_habit_is_reminded_with_todays_due_time(self):
        tasks, registry = Tasks(FakeDb(), CHICAGO), Registry()
        todo.register(registry, tasks)
        run(tasks.add(42, 'Stretch', '', '2026-10-15T07:30', '2026-10-15T07:00', 'daily', [], now=NOW))
        run(tasks.fire_due(datetime(2026, 10, 15, 12, 0, tzinfo=UTC)))  # Thu's fired; never done

        [(_, text)] = run(registry.poll(datetime(2026, 10, 16, 12, 0, tzinfo=UTC)))

        self.assertIn('due Fri 16 Oct 07:30', text)


class FakeBot:

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(chat_id)


class FakeAgent:

    def __init__(self, messages):
        self.informed = []
        self.registry = SimpleNamespace(poll=self._poll)
        self.messages = messages

    async def _poll(self, now):
        return self.messages

    async def inform(self, chat_id, text):
        self.informed.append((chat_id, text))


class Ticker(unittest.TestCase):

    def test_a_failing_tick_does_not_stop_the_timer(self):
        calls = []

        async def poll(now):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError('mongo is down')
            raise asyncio.CancelledError

        agent = SimpleNamespace(registry=SimpleNamespace(poll=poll))

        with self.assertLogs(channel.log, level='ERROR'), self.assertRaises(asyncio.CancelledError):
            run(channel.tick(FakeBot(), agent, frozenset({1}), every=0))

        self.assertEqual(len(calls), 2)

    def test_sent_and_recorded_but_only_to_allowed_chats(self):
        bot, agent = FakeBot(), FakeAgent([(1, 'remind one'), (99, 'remind stranger')])

        with self.assertLogs(channel.log, level='WARNING'):
            run(channel.tick_once(bot, agent, frozenset({1})))

        self.assertEqual(bot.sent, [1])
        self.assertEqual(agent.informed, [(1, 'remind one')])


if __name__ == '__main__':
    unittest.main()
