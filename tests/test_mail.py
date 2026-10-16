''' the Gmail capability: what Claude reads, what a draft contains, and what asks first

No network: Gmail is a fake that records calls. The traps: an HTML email read
as tags or scripts, a reply that lands outside its thread or goes to the wrong
person, a send prompt that hides who it goes to, and anything that leaves or
removes mail without a yes.
'''

import asyncio
import base64
import email
import unittest
from zoneinfo import ZoneInfo

from shelly.capabilities import mail as mail_module
from shelly.capabilities.mail import body, register
from shelly.core.tools import Registry
from shelly.integrations.google import GoogleError

TZ = ZoneInfo('America/Chicago')


def run(coroutine):
    return asyncio.run(coroutine)


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def message(id, sender='Ana <ana@example.com>', subject='Lunch', text='See you at noon.',
            labels=('INBOX', 'UNREAD'), extra_headers=(), payload=None, thread='t1'):
    head = [{'name': 'From', 'value': sender}, {'name': 'To', 'value': 'me@example.com'},
            {'name': 'Subject', 'value': subject}, *extra_headers]
    return {'id': id, 'threadId': thread, 'labelIds': list(labels), 'internalDate': '1792087200000',
            'payload': payload or {'mimeType': 'text/plain', 'headers': head, 'body': {'data': b64(text)}}
            | {'headers': head}}


class FakeGmail:

    def __init__(self, threads=None, messages=None, drafts=None):
        self.threads_ = threads or {}
        self.messages = messages or {}
        self.drafts = drafts or {}
        self.calls = []

    async def threads(self, query, limit):
        self.calls.append(('threads', query, limit))
        return [{'id': tid, 'snippet': 'See you &amp; bye'} for tid in list(self.threads_)[:limit]]

    async def thread(self, thread_id, full=True):
        if thread_id not in self.threads_:
            raise GoogleError('Google said 404: Not Found')
        return {'id': thread_id, 'messages': self.threads_[thread_id]}

    async def message(self, message_id):
        return self.messages[message_id]

    async def create_draft(self, raw, thread_id=None):
        self.calls.append(('draft', raw, thread_id))
        return {'id': 'd1'}

    async def draft(self, draft_id):
        return self.drafts[draft_id]

    async def send_draft(self, draft_id):
        self.calls.append(('send', draft_id))
        return {'id': 'm9'}

    async def modify(self, thread_id, add=(), remove=()):
        self.calls.append(('modify', thread_id, list(add), list(remove)))

    async def trash(self, thread_id):
        self.calls.append(('trash', thread_id))


class Setup(unittest.TestCase):

    def make(self, **kwargs):
        self.gmail = FakeGmail(**kwargs)
        self.registry = Registry()
        register(self.registry, self.gmail, TZ)

    def tool(self, name):
        return self.registry._tools[name]


class Bodies(unittest.TestCase):

    def test_plain_text_is_preferred_and_quotes_are_dropped(self):
        payload = {'mimeType': 'multipart/alternative', 'parts': [
            {'mimeType': 'text/plain', 'body': {'data': b64('Yes, Friday works.\n\n> earlier message\n> more')}},
            {'mimeType': 'text/html', 'body': {'data': b64('<p>Yes</p>')}},
        ]}
        self.assertEqual(body(payload), ('Yes, Friday works.', []))

    def test_html_alone_is_read_as_text_without_scripts_or_styles(self):
        payload = {'mimeType': 'text/html', 'body': {'data': b64(
            '<html><head><style>p{}</style></head><body><p>Hello&nbsp;there</p>'
            '<script>evil()</script><div>Second line</div></body></html>')}}
        text, _ = body(payload)

        self.assertEqual(text, 'Hello\xa0there\nSecond line')

    def test_attachments_are_named_not_read(self):
        payload = {'mimeType': 'multipart/mixed', 'parts': [
            {'mimeType': 'text/plain', 'body': {'data': b64('Invoice attached.')}},
            {'mimeType': 'application/pdf', 'filename': 'invoice.pdf', 'body': {'attachmentId': 'a1'}},
        ]}
        self.assertEqual(body(payload), ('Invoice attached.', ['invoice.pdf']))


class Reading(Setup):

    def test_only_sending_and_organising_ask_and_none_are_strict(self):
        self.make()

        asks = {name for name, tool in self.registry._tools.items() if tool.confirm}
        self.assertEqual(asks, {'send_draft', 'organise_mail'})
        self.assertEqual({t['strict'] for t in self.registry.params()}, {False})

    def test_search_lists_threads_with_flags_ids_and_a_warning(self):
        self.make(threads={'t1': [message('m1')]})
        text = run(self.tool('search_mail').handler(query='from:ana', limit=500))

        self.assertTrue(text.startswith(mail_module.UNTRUSTED))
        self.assertIn('Ana <ana@example.com> · "Lunch" · 1 message · unread, in inbox [thread: t1]', text)
        self.assertIn('See you & bye', text)
        self.assertEqual(self.gmail.calls[0], ('threads', 'from:ana', mail_module.MAX_RESULTS))

    def test_nothing_found_says_so(self):
        self.make()
        self.assertEqual(run(self.tool('search_mail').handler(query='is:starred', limit=5)),
                         "No mail matching 'is:starred'.")

    def test_a_long_thread_shows_its_latest_messages_clipped(self):
        many = [message(f'm{i}', text='x' * 5000 if i == 11 else f'note {i}') for i in range(12)]
        self.make(threads={'t1': many})
        text = run(self.tool('read_thread').handler(thread_id='t1'))

        self.assertIn('showing the last 10 of 12', text)
        self.assertNotIn('[message: m1]', text)
        self.assertIn('[message: m11]', text)
        self.assertIn(' […]', text)


def parsed(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


ORIGINAL = message('m1', sender='Ana <ana@example.com>', subject='Lunch Friday?', thread='t7',
                   extra_headers=[{'name': 'Message-ID', 'value': '<abc@mail>'},
                                  {'name': 'Reply-To', 'value': 'Ana Work <ana@work.example>'}])


class Drafting(Setup):

    def test_a_reply_joins_the_thread_and_answers_the_reply_to_address(self):
        self.make(messages={'m1': ORIGINAL})
        result = run(self.tool('create_draft').handler(to=[], cc=[], subject='', body='Friday works.', reply_to='m1'))
        _, raw, thread = self.gmail.calls[-1]
        sent = parsed(raw)

        self.assertEqual(thread, 't7')
        self.assertEqual(sent['To'], 'ana@work.example')
        self.assertEqual(sent['Subject'], 'Re: Lunch Friday?')
        self.assertEqual((sent['In-Reply-To'], sent['References']), ('<abc@mail>', '<abc@mail>'))
        self.assertIn('It has not been sent', result)
        self.assertIn('[draft: d1]', result)

    def test_re_is_not_doubled(self):
        self.make(messages={'m1': message('m1', subject='Re: Lunch')})
        run(self.tool('create_draft').handler(to=[], cc=[], subject='', body='ok', reply_to='m1'))
        self.assertEqual(parsed(self.gmail.calls[-1][1])['Subject'], 'Re: Lunch')

    def test_a_new_email_needs_a_real_recipient(self):
        self.make()
        with self.assertRaises(GoogleError):
            run(self.tool('create_draft').handler(to=[], cc=[], subject='Hi', body='x', reply_to=''))
        with self.assertRaises(GoogleError):
            run(self.tool('create_draft').handler(to=['ana'], cc=[], subject='Hi', body='x', reply_to=''))


class Sending(Setup):

    def test_the_prompt_shows_who_what_and_the_text(self):
        draft = {'id': 'd1', 'message': message('m5', subject='Running late', text='Ten minutes behind, sorry.')}
        draft['message']['payload']['headers'] = [{'name': 'To', 'value': 'ana@example.com'},
                                                  {'name': 'Cc', 'value': 'bo@example.com'},
                                                  {'name': 'Subject', 'value': 'Running late'}]
        self.make(drafts={'d1': draft})
        action = run(self.tool('send_draft').confirm({'draft_id': 'd1'}))

        self.assertEqual(action, 'Send this email from your Gmail\nTo: ana@example.com\nCc: bo@example.com\n'
                                 'Subject: Running late\n\nTen minutes behind, sorry.')

    def test_send_sends_that_draft(self):
        self.make()
        self.assertEqual(run(self.tool('send_draft').handler(draft_id='d1')), 'Sent [message: m9].')
        self.assertEqual(self.gmail.calls, [('send', 'd1')])


class Organising(Setup):

    def test_archive_and_trash_do_what_they_say(self):
        self.make(threads={'t1': [message('m1')], 't2': [message('m2')]})
        run(self.tool('organise_mail').handler(thread_ids=['t1'], action='archive'))
        run(self.tool('organise_mail').handler(thread_ids=['t2'], action='trash'))

        self.assertEqual(self.gmail.calls, [('modify', 't1', [], ['INBOX']), ('trash', 't2')])

    def test_the_prompt_names_every_thread(self):
        self.make(threads={'t1': [message('m1')], 't2': [message('m2', sender='bo@example.com', subject='Invoice')]})
        action = run(self.tool('organise_mail').confirm({'thread_ids': ['t1', 't2'], 'action': 'trash'}))

        self.assertEqual(action, 'Move to trash 2 threads: "Lunch" from Ana; "Invoice" from bo@example.com')

    def test_too_many_or_none_or_a_bad_action_are_refused(self):
        self.make()
        for ids, action in ((['t'] * 26, 'archive'), ([], 'archive'), (['t1'], 'delete_forever')):
            with self.assertRaises(GoogleError):
                run(self.tool('organise_mail').confirm({'thread_ids': ids, 'action': action}))


if __name__ == '__main__':
    unittest.main()
