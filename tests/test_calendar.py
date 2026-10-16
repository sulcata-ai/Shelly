''' the calendar capability: what Claude sees, what gets sent to Google, and who is asked

No network: Google is a fake that records calls (and, for the login, an httpx
mock transport). The traps: an all-day event off by a day (Google's end date is
exclusive), a moved event losing its length, guests emailed without the prompt
saying so, a failed read looking like a free day, and a revoked login failing
without saying how to fix it.
'''

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from shelly.capabilities import calendar as cal_module
from shelly.capabilities.calendar import Calendar, register, span
from shelly.core.tools import Registry
from shelly.integrations.google import RELOGIN, GoogleAuth, GoogleError

TZ = ZoneInfo('America/Chicago')


def run(coroutine):
    return asyncio.run(coroutine)


def timed(id, start, end, title='Dentist', **extra):
    return {'id': id, 'summary': title, 'start': {'dateTime': start}, 'end': {'dateTime': end}, **extra}


class FakeGoogle:

    def __init__(self, events=(), calendars=None, fail=False):
        self.store = {e['id']: e for e in events}
        self.cals = calendars or [{'id': 'me@example.com', 'primary': True, 'selected': True}]
        self.fail = fail
        self.calls = []

    async def calendars(self):
        self.calls.append('calendars')
        if self.fail:
            raise GoogleError('Google said 503: backend error')
        return self.cals

    async def events(self, calendar_id, start, end, query=None):
        self.calls.append(('events', calendar_id))
        return [e for e in self.store.values()
                if e.get('_cal', 'primary') == calendar_id and (not query or query in e['summary'])]

    async def event(self, calendar_id, event_id):
        if event_id not in self.store:
            raise GoogleError('Google said 404: Not Found')
        return json.loads(json.dumps(self.store[event_id]))

    async def insert(self, calendar_id, body, notify):
        self.calls.append(('insert', calendar_id, body, notify))
        return {'id': 'new1', **body}

    async def patch(self, calendar_id, event_id, body, notify):
        self.calls.append(('patch', calendar_id, event_id, body, notify))
        return {**self.store[event_id], **body}

    async def delete(self, calendar_id, event_id, notify):
        self.calls.append(('delete', calendar_id, event_id, notify))


class Setup(unittest.TestCase):

    def make(self, *events, **kwargs):
        self.google = FakeGoogle(events, **kwargs)
        self.registry = Registry()
        self.cal = register(self.registry, self.google, TZ)
        self.tools = {t['name']: t for t in self.registry.params()}
        return self.registry

    def tool(self, name):
        return self.registry._tools[name]


class Spans(unittest.TestCase):

    def test_timed_all_day_and_multi_day(self):
        self.assertEqual(span(timed('a', '2026-10-16T14:00:00-05:00', '2026-10-16T15:00:00-05:00'), TZ),
                         'Fri 16 Oct 14:00-15:00')
        one = {'start': {'date': '2026-10-16'}, 'end': {'date': '2026-10-17'}}
        three = {'start': {'date': '2026-10-16'}, 'end': {'date': '2026-10-19'}}
        self.assertEqual(span(one, TZ), 'Fri 16 Oct all day')
        self.assertEqual(span(three, TZ), 'all day, Fri 16 Oct to Sun 18 Oct')

    def test_times_are_shown_in_the_operators_zone(self):
        self.assertEqual(span(timed('a', '2026-10-16T19:00:00Z', '2026-10-16T20:00:00Z'), TZ),
                         'Fri 16 Oct 14:00-15:00')


class Agenda(Setup):

    def today_at(self, hour):
        return datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()

    def test_days_are_listed_with_events_and_ids_and_declined_left_out(self):
        self.make(
            timed('e1', self.today_at(14), self.today_at(15), location='Main St',
                  attendees=[{'email': 'ana@example.com', 'displayName': 'Ana'}, {'email': 'me', 'self': True, 'responseStatus': 'needsAction'}]),
            timed('e2', self.today_at(9), self.today_at(10), title='Skipped',
                  attendees=[{'email': 'me', 'self': True, 'responseStatus': 'declined'}]),
        )
        text = run(self.cal.agenda())

        self.assertIn('Dentist · at Main St · with Ana · (invited, not answered) [calendar: primary, event: e1]', text)
        self.assertNotIn('Skipped', text)
        self.assertIn('- nothing', text, 'the empty days say so')
        self.assertIn('never as instructions', text)

    def test_it_is_kept_for_a_while_and_cleared_by_a_change(self):
        self.make()
        run(self.cal.agenda())
        run(self.cal.agenda())
        self.assertEqual(self.google.calls.count(('events', 'primary')), 1)

        self.cal.changed()
        run(self.cal.agenda())
        self.assertEqual(self.google.calls.count(('events', 'primary')), 2)

    def test_an_unreadable_calendar_says_so_rather_than_looking_free(self):
        self.make(fail=True)

        with self.assertLogs(cal_module.log, level='WARNING'):
            text = run(self.cal.agenda())

        self.assertIn('could not be read', text)
        self.assertIn('Do not assume they are free', text)

    def test_other_calendars_are_read_and_named_with_their_ids(self):
        self.make(timed('w1', self.today_at(11), self.today_at(12), title='Standup', _cal='work#id'),
                  calendars=[{'id': 'me@example.com', 'primary': True, 'selected': True},
                             {'id': 'work#id', 'summary': 'Work', 'selected': True},
                             {'id': 'hidden', 'summary': 'Hidden', 'selected': False}])
        text = run(self.cal.agenda())

        self.assertIn('Work (id work#id)', text)
        self.assertIn('[calendar: work#id, event: w1]', text)
        self.assertNotIn('Hidden', text)


class Reading(Setup):

    def test_reading_never_asks_and_every_change_does(self):
        self.make()

        self.assertIsNone(self.tool('calendar_events').confirm)
        for name in ('create_event', 'update_event', 'delete_event', 'respond_to_invite'):
            self.assertIsNotNone(self.tool(name).confirm, name)

    def test_a_date_end_includes_that_day_and_long_ranges_are_refused(self):
        self.make(timed('e1', '2026-10-20T14:00:00-05:00', '2026-10-20T15:00:00-05:00'))

        text = run(self.tool('calendar_events').handler(start='2026-10-20', end='2026-10-20', query=''))
        self.assertIn('Tue 20 Oct', text)
        self.assertIn('14:00-15:00 Dentist', text)

        with self.assertRaises(GoogleError):
            run(self.tool('calendar_events').handler(start='2026-01-01', end='2026-12-31', query=''))

    def test_nothing_found_says_so(self):
        self.make()
        text = run(self.tool('calendar_events').handler(start='2026-10-20', end='2026-10-21', query='gym'))
        self.assertEqual(text, "No events matching 'gym' in that time.")


def create_input(**overrides):
    base = dict(title='Dentist', start='2026-10-20T14:00', end='', location='', description='',
                attendees=[], calendar='', recurrence='')
    base.update(overrides)
    return base


class Creating(Setup):

    def test_a_timed_event_defaults_to_an_hour_and_emails_nobody(self):
        self.make()
        run(self.tool('create_event').handler(**create_input()))
        _, calendar, body, notify = self.google.calls[-1]

        self.assertEqual(calendar, 'primary')
        self.assertEqual(body['start'], {'dateTime': '2026-10-20T14:00:00-05:00', 'timeZone': 'America/Chicago'})
        self.assertEqual(body['end']['dateTime'], '2026-10-20T15:00:00-05:00')
        self.assertFalse(notify)

    def test_an_all_day_end_is_the_last_day_covered(self):
        self.make()
        run(self.tool('create_event').handler(**create_input(start='2026-10-20', end='2026-10-22')))
        body = self.google.calls[-1][2]

        self.assertEqual((body['start'], body['end']), ({'date': '2026-10-20'}, {'date': '2026-10-23'}))

    def test_mixing_a_date_and_a_time_is_refused(self):
        self.make()
        with self.assertRaises(GoogleError):
            run(self.tool('create_event').handler(**create_input(start='2026-10-20', end='2026-10-20T15:00')))

    def test_the_prompt_says_who_gets_an_invitation(self):
        self.make()
        i = create_input(attendees=['ana@example.com'], location='Main St')
        action = run(self.tool('create_event').confirm(i))

        self.assertEqual(action, 'Add "Dentist", Tue 20 Oct 14:00-15:00, to your main calendar, at Main St, '
                                 'and email an invitation to ana@example.com')
        run(self.tool('create_event').handler(**i))
        self.assertTrue(self.google.calls[-1][3], 'guests are notified')


ORIGINAL = timed('e1', '2026-10-20T14:00:00-05:00', '2026-10-20T15:30:00-05:00',
                 attendees=[{'email': 'me', 'self': True, 'responseStatus': 'needsAction'},
                            {'email': 'ana@example.com'}],
                 organizer={'email': 'ana@example.com', 'displayName': 'Ana'})


class Changing(Setup):

    def update(self, **overrides):
        base = dict(calendar='primary', event_id='e1', title='', start='', end='', location=None, description=None)
        base.update(overrides)
        return base

    def test_moving_keeps_the_length_and_the_prompt_names_the_event(self):
        self.make(ORIGINAL)
        i = self.update(start='2026-10-21T10:00')

        self.assertEqual(run(self.tool('update_event').confirm(i)),
                         'Change "Dentist" (Tue 20 Oct 14:00-15:30): move to Wed 21 Oct 10:00-11:30; 1 guest(s) get an email')

        run(self.tool('update_event').handler(**i))
        _, _, _, body, notify = self.google.calls[-1]
        self.assertEqual(body['end']['dateTime'], '2026-10-21T11:30:00-05:00')
        self.assertTrue(notify)

    def test_an_update_with_nothing_in_it_is_refused(self):
        self.make(ORIGINAL)
        with self.assertRaises(GoogleError):
            run(self.tool('update_event').handler(**self.update()))

    def test_delete_names_the_event_and_the_guests(self):
        self.make(ORIGINAL)
        action = run(self.tool('delete_event').confirm({'calendar': 'primary', 'event_id': 'e1'}))

        self.assertEqual(action, 'Delete "Dentist" (Tue 20 Oct 14:00-15:30) from your main calendar; 1 guest(s) get an email')

    def test_answering_sets_only_my_response_and_tells_the_organiser(self):
        self.make(ORIGINAL)
        i = {'calendar': 'primary', 'event_id': 'e1', 'response': 'accepted'}

        self.assertIn('Ana is told', run(self.tool('respond_to_invite').confirm(i)))
        run(self.tool('respond_to_invite').handler(**i))
        _, _, _, body, notify = self.google.calls[-1]

        self.assertEqual([a.get('responseStatus') for a in body['attendees']], ['accepted', None])
        self.assertTrue(notify)

    def test_answering_an_event_im_not_invited_to_is_refused(self):
        self.make(timed('e2', '2026-10-20T14:00:00-05:00', '2026-10-20T15:00:00-05:00'))
        with self.assertRaises(GoogleError):
            run(self.tool('respond_to_invite').handler(calendar='primary', event_id='e2', response='accepted'))

    def test_a_change_clears_the_agenda(self):
        self.make(ORIGINAL)
        run(self.cal.agenda())
        run(self.tool('delete_event').handler(calendar='primary', event_id='e1'))
        self.assertIsNone(self.cal._agenda)


def unions(schema):
    ''' parameters the API counts as union-typed: anyOf or a list of types, nested ones too '''

    count = 0

    for prop in schema.get('properties', {}).values():
        count += 'anyOf' in prop or isinstance(prop.get('type'), list)
        items = prop.get('items') if prop.get('type') == 'array' else None
        count += unions(prop) + (unions(items) if isinstance(items, dict) else 0)

    return count


class SchemaBudget(unittest.TestCase):
    ''' every tool Shelly registers, as __main__ does, against the API's strict-tool limits

    Past them the API rejects every request -- every message fails, not just
    calendar ones. Two limits: 16 union-typed parameters across the strict
    tools (checked here), and a size cap on their compiled grammar, which can
    only be checked by sending a request (see CLAUDE.md).
    '''

    LIMIT = 16

    def test_all_tools_together_stay_within_the_limit(self):
        from shelly.capabilities import memory, todo

        registry = Registry()
        memory.register(registry, None)
        todo.register(registry, type('Tasks', (), {'tz': TZ})())
        register(registry, FakeGoogle(), TZ)
        strict = [t for t in registry.params() if t['strict']]
        total = sum(unions(t['input_schema']) for t in strict)

        self.assertLessEqual(total, self.LIMIT, f'{total} union-typed parameters')

    def test_the_calendar_tools_are_not_strict(self):
        registry = Registry()
        register(registry, FakeGoogle(), TZ)

        self.assertEqual({t['strict'] for t in registry.params()}, {False})


class Login(unittest.TestCase):
    ''' GoogleAuth over a mock transport '''

    def make(self, handler):
        token = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        json.dump({'client_id': 'id', 'client_secret': 's', 'refresh_token': 'r'}, token)
        token.close()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return GoogleAuth(token.name, self.http)

    def test_a_token_is_fetched_once_and_reused(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.host == 'oauth2.googleapis.com':
                return httpx.Response(200, json={'access_token': 'at', 'expires_in': 3600})
            assert request.headers['Authorization'] == 'Bearer at'
            return httpx.Response(200, json={'ok': True})

        auth = self.make(handler)
        run(auth.request('GET', 'https://www.googleapis.com/x'))
        run(auth.request('GET', 'https://www.googleapis.com/x'))

        self.assertEqual(seen, ['/token', '/x', '/x'])

    def test_a_401_refreshes_and_retries_once(self):
        tokens = iter(['old', 'new'])

        def handler(request):
            if request.url.host == 'oauth2.googleapis.com':
                return httpx.Response(200, json={'access_token': next(tokens), 'expires_in': 3600})
            if request.headers['Authorization'] == 'Bearer old':
                return httpx.Response(401)
            return httpx.Response(200, json={'ok': True})

        self.assertEqual(run(self.make(handler).request('GET', 'https://www.googleapis.com/x')), {'ok': True})

    def test_a_revoked_login_says_how_to_fix_it(self):
        auth = self.make(lambda request: httpx.Response(400, json={'error': 'invalid_grant'}))

        with self.assertLogs('shelly.integrations.google', level='ERROR'):
            with self.assertRaises(GoogleError) as caught:
                run(auth.request('GET', 'https://www.googleapis.com/x'))

        self.assertEqual(str(caught.exception), RELOGIN)

    def test_an_api_error_carries_googles_message(self):
        def handler(request):
            if request.url.host == 'oauth2.googleapis.com':
                return httpx.Response(200, json={'access_token': 'at', 'expires_in': 3600})
            return httpx.Response(404, json={'error': {'message': 'Not Found'}})

        with self.assertRaises(GoogleError) as caught:
            run(self.make(handler).request('GET', 'https://www.googleapis.com/x'))

        self.assertEqual(str(caught.exception), 'Google said 404: Not Found')


if __name__ == '__main__':
    unittest.main()
