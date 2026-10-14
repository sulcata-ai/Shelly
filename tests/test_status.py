''' /status: the numbers it reports, and what it says when the database is down '''

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from shelly.status import Status, _duration, _money

CHICAGO = ZoneInfo('America/Chicago')


class FakeNotes:

    def __init__(self, n=0, fail=False):
        self.n, self.fail = n, fail

    async def all(self):
        if self.fail:
            raise ConnectionError('down')
        return [{}] * self.n


class FakeLedger:

    def __init__(self):
        self.asked = []

    async def spent_since(self, since):
        self.asked.append(since)
        return (3, 0.042) if len(self.asked) == 1 else (20, 0.31)


class FakeHistory:

    async def window(self, chat_id):
        return [{}] * 4


def agent():
    return SimpleNamespace(model='claude-opus-5-5', effort='medium', history=FakeHistory())


def run(coroutine):
    return asyncio.run(coroutine)


class Report(unittest.TestCase):

    def test_the_full_report(self):
        ledger = FakeLedger()
        status = Status(agent(), FakeNotes(2), ledger, CHICAGO)
        text = run(status.report(1, now=datetime(2026, 10, 14, 16, 30, tzinfo=CHICAGO)))

        self.assertIn('**Model** claude-opus-5-5, effort medium', text)
        self.assertIn('**In view** 4 messages', text)
        self.assertIn('**Notes** 2', text)
        self.assertIn('**Spend** today $0.042 over 3 turns, last 7 days $0.31', text)

    def test_today_starts_at_local_midnight_and_the_week_six_days_before(self):
        ledger = FakeLedger()
        run(Status(agent(), FakeNotes(), ledger, CHICAGO).report(1, now=datetime(2026, 10, 14, 0, 30, tzinfo=CHICAGO)))

        self.assertEqual(ledger.asked, [datetime(2026, 10, 14, tzinfo=CHICAGO), datetime(2026, 10, 8, tzinfo=CHICAGO)])

    def test_an_unreachable_database_is_reported_not_raised(self):
        text = run(Status(agent(), FakeNotes(fail=True), FakeLedger(), CHICAGO).report(1))

        self.assertIn('**Database** unreachable: ConnectionError', text)
        self.assertIn('**Model**', text)


class Formatting(unittest.TestCase):

    def test_durations(self):
        self.assertEqual([_duration(s) for s in (59, 600, 7500, 200000)], ['0m', '10m', '2h 5m', '2d 7h'])

    def test_money(self):
        self.assertEqual([_money(x) for x in (0, None, 0.0042, 0.31, 12.5)], ['$0', '$0', '$0.004', '$0.31', '$12.50'])


if __name__ == '__main__':
    unittest.main()
