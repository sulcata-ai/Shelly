''' money: the SimpleFIN client, the synced store, the sync poller, the tools

No network: the Bridge is either an httpx MockTransport or a scripted fake.
The traps: credentials leaking into a URL, a sync on every tick (the Bridge
disables tokens past ~24 requests a day), a pending charge counted twice once
it posts, a card payment counted as spending, and the same bank error or
lapsed subscription told over and over.
'''

import asyncio
import base64
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from shelly.capabilities import finance
from shelly.core.tools import Registry
from shelly.integrations import simplefin
from shelly.integrations.simplefin import SimpleFin, SimpleFinError
from shelly.memory.finance import Finance
from tests.fakes import FakeDb

TZ = ZoneInfo('America/Chicago')
CHAT = 1111
NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)   # Fri 12:00 local


def run(coroutine):
    return asyncio.run(coroutine)


def epoch(moment):
    return int(moment.timestamp())


def tx(id, amount, when, description='Coffee', pending=False, **extra):
    return {'id': id, 'posted': 0 if pending else epoch(when), 'transacted_at': epoch(when),
            'amount': amount, 'description': description, 'pending': pending, **extra}


def account(id, name, balance, transactions=(), **extra):
    return {'id': id, 'name': name, 'currency': 'USD', 'balance': balance,
            'balance-date': epoch(NOW), 'org': {'name': 'Bank'}, 'transactions': list(transactions), **extra}


class FakeBridge:
    ''' SimpleFin's interface: hands out scripted account sets and records each request '''

    def __init__(self, *sets, error=None):
        self.sets = list(sets)
        self.error = error
        self.calls = []

    async def accounts(self, start=None, end=None, balances_only=False):
        self.calls.append((start, end))

        if self.error:
            raise self.error

        return self.sets.pop(0) if len(self.sets) > 1 else self.sets[0]


class Client(unittest.TestCase):

    def test_a_setup_token_holds_a_claim_url(self):
        token = base64.b64encode(b'https://bridge.simplefin.org/simplefin/claim/abc').decode()

        self.assertEqual(simplefin.claim_url(token + '\n'), 'https://bridge.simplefin.org/simplefin/claim/abc')

    def test_a_token_that_is_not_base64_or_not_https_is_refused(self):
        with self.assertRaises(SimpleFinError):
            simplefin.claim_url('not a token!')

        with self.assertRaises(SimpleFinError):
            simplefin.claim_url(base64.b64encode(b'http://example.com/claim').decode())

    def test_a_claimed_token_says_it_may_be_compromised(self):
        token = base64.b64encode(b'https://bridge.example/claim/x').decode()
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403)))

        with self.assertRaises(SimpleFinError) as caught:
            run(simplefin.claim(token, http))

        self.assertIn('someone else', str(caught.exception))

    def test_credentials_go_in_the_header_not_the_url(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'errors': [], 'accounts': []})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        api = SimpleFin('https://user:secret@bridge.example/simplefin', http)
        run(api.accounts(start=NOW))

        request = seen[0]
        self.assertNotIn('secret', str(request.url))
        self.assertEqual(request.url.path, '/simplefin/accounts')
        self.assertEqual(request.url.params['start-date'], str(epoch(NOW)))
        self.assertEqual(request.url.params['pending'], '1')
        self.assertEqual(request.headers['authorization'],
                         'Basic ' + base64.b64encode(b'user:secret').decode())

    def test_payment_and_access_problems_are_told_apart(self):
        for status, words in ((402, 'subscription'), (403, 'revoked')):
            http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request, s=status: httpx.Response(s)))

            with self.assertRaises(SimpleFinError) as caught:
                run(SimpleFin('https://u:p@bridge.example/simplefin', http).accounts())

            self.assertEqual(caught.exception.status, status)
            self.assertIn(words, str(caught.exception))

    def test_errors_from_either_protocol_version(self):
        self.assertEqual(simplefin.errors({'errors': ['Re-link Chase']}), ['Re-link Chase'])
        self.assertEqual(simplefin.errors({'errlist': [{'code': 'con.auth', 'msg': 'Sign in again'}]}),
                         ['Sign in again'])


class Store(unittest.TestCase):

    def setUp(self):
        self.store = Finance(FakeDb())

    def save(self, *accounts, start=NOW - timedelta(days=10)):
        return run(self.store.save({'accounts': list(accounts)}, {}, start, NOW))

    def test_a_pending_charge_is_replaced_by_what_it_posts_as(self):
        day = NOW - timedelta(days=1)
        self.save(account('a', 'Checking', '10', [tx('p1', '-5.00', day, pending=True)]))
        self.save(account('a', 'Checking', '5', [tx('t9', '-5.00', day)]))

        docs = run(self.store.transactions(NOW - timedelta(days=5), NOW))

        self.assertEqual([d['_id'] for d in docs], ['a:t9'])
        self.assertFalse(docs[0]['pending'])

    def test_a_pending_charge_uses_when_it_happened(self):
        day = NOW - timedelta(days=1)
        self.save(account('a', 'Checking', '10', [tx('p1', '-5.00', day, pending=True)]))

        doc = run(self.store.transactions(NOW - timedelta(days=5), NOW))[0]

        self.assertEqual(doc['at'], day)
        self.assertIsNone(doc['posted_at'])

    def test_a_transaction_with_no_time_is_skipped(self):
        accounts, saved = self.save(account('a', 'Checking', '10', [{'id': 'x', 'posted': 0, 'amount': '-1',
                                                                      'description': 'Mystery'}]))

        self.assertEqual((accounts, saved), (1, 0))

    def test_payee_memo_mcc_and_holdings_are_kept(self):
        holding = {'symbol': 'AAPL', 'market_value': '100.5', 'shares': '1'}
        self.save(account('a', 'Brokerage', '100.5', [tx('t', '-3', NOW - timedelta(hours=1), payee='Blue Bottle',
                                                          memo='BLUE BOTTLE #12', mcc='5814')],
                          holdings=[holding]))

        doc = run(self.store.transactions(NOW - timedelta(days=1), NOW))[0]
        saved = run(self.store.accounts())[0]

        self.assertEqual((doc['payee'], doc['memo'], doc['mcc']), ('Blue Bottle', 'BLUE BOTTLE #12', '5814'))
        self.assertEqual(saved['holdings'][0]['symbol'], 'AAPL')
        self.assertEqual(saved['institution'], 'Bank')


class Sync(unittest.TestCase):

    def setUp(self):
        self.store = Finance(FakeDb())
        self.registry = Registry()

    def register(self, bridge, chat=CHAT):
        self.bridge = bridge
        finance.register(self.registry, bridge, self.store, TZ, chat)

    def test_the_first_sync_reaches_back_just_under_ninety_days_in_45_day_requests(self):
        self.register(FakeBridge({'accounts': []}))
        run(self.registry.poll(NOW))

        self.assertEqual(self.bridge.calls, [(NOW - timedelta(days=89), NOW - timedelta(days=44)),
                                             (NOW - timedelta(days=44), None)])

    def test_it_syncs_every_six_hours_with_a_five_day_overlap(self):
        self.register(FakeBridge({'accounts': []}))

        for minutes in range(0, 6 * 60, 7):
            run(self.registry.poll(NOW + timedelta(minutes=minutes)))

        self.assertEqual(len(self.bridge.calls), 2)   # the first sync's two chunks

        run(self.registry.poll(NOW + timedelta(hours=6)))

        self.assertEqual(self.bridge.calls[2], (NOW - timedelta(days=5), None))

    def test_a_restart_does_not_sync_again_early(self):
        self.register(FakeBridge({'accounts': []}))
        run(self.registry.poll(NOW))
        again = Registry()
        finance.register(again, self.bridge, self.store, TZ, CHAT)

        run(again.poll(NOW + timedelta(hours=1)))

        self.assertEqual(len(self.bridge.calls), 2)

    def test_a_bank_error_is_told_once(self):
        self.register(FakeBridge({'accounts': [], 'errors': ['Chase needs you to sign in again']}))

        first = run(self.registry.poll(NOW))
        second = run(self.registry.poll(NOW + timedelta(hours=6)))

        self.assertEqual(len(first), 1)
        self.assertIn('Chase needs you to sign in again', first[0][1])
        self.assertEqual(second, [])

    def test_a_lapsed_subscription_is_told_once_and_retried_later(self):
        self.register(FakeBridge(error=SimpleFinError('Payment required.', 402)))

        first = run(self.registry.poll(NOW))
        soon = run(self.registry.poll(NOW + timedelta(minutes=5)))
        later = run(self.registry.poll(NOW + timedelta(minutes=31)))

        self.assertEqual(len(first), 1)
        self.assertIn('Bank sync stopped', first[0][1])
        self.assertEqual((soon, later), ([], []))
        self.assertEqual(len(self.bridge.calls), 2)

    def test_without_a_chat_it_still_syncs(self):
        self.register(FakeBridge({'accounts': [], 'errors': ['Re-link']}), chat=None)

        self.assertEqual(run(self.registry.poll(NOW)), [])
        self.assertEqual(len(self.bridge.calls), 2)


class Tools(unittest.TestCase):

    def setUp(self):
        self.store = Finance(FakeDb())
        self.registry = Registry()
        day = datetime.now(timezone.utc) - timedelta(days=2)
        checking = account('c', 'Everyday Checking', '1000.00', [
            tx('1', '-500.00', day, 'Payment to card'),
            tx('2', '-42.10', day, 'WHOLE FOODS #123', mcc='5411'),
            tx('3', '2000.00', day, 'ACME PAYROLL'),
        ], **{'available-balance': '950.00'})
        card = account('v', 'Sapphire Card', '-300.00', [
            tx('4', '500.00', day + timedelta(days=1), 'Thank you payment'),
            tx('5', '-12.00', day, 'SQ *BLUE BOTTLE 10/14', mcc='5814'),
            tx('6', '-8.00', day, 'Blue Bottle Coffee', payee='Blue Bottle', mcc='5814', pending=True),
        ])
        run(self.store.save({'accounts': [checking, card]}, {}, day - timedelta(days=30), day))
        run(self.store.save_state({'synced_at': day, 'errors': [], 'history_from': day - timedelta(days=30)}))
        finance.register(self.registry, FakeBridge({'accounts': []}), self.store, TZ, CHAT)

    def call(self, name, **args):
        text, error = run(self.registry.run(name, args))
        self.assertFalse(error, text)
        return text

    def test_balances_are_in_the_prompt(self):
        block = run(self.registry.context())[0]

        self.assertIn('Bank: Everyday Checking: balance $1,000.00, available $950.00', block)
        self.assertIn('Sapphire Card: balance -$300.00', block)
        self.assertIn('read-only', block)

    def test_nothing_synced_yet_says_so(self):
        registry = Registry()
        finance.register(registry, FakeBridge({'accounts': []}), Finance(FakeDb()), TZ, CHAT)

        self.assertIn('nothing has synced yet', run(registry.context())[0])

    def test_transactions_filter_by_account_text_and_direction(self):
        text = self.call('finance_transactions', start='', end='', account='sapphire', query='blue',
                         direction='out', limit=50, category='')

        self.assertIn('2 transactions', text)
        self.assertIn('-$12.00', text)
        self.assertIn('(pending)', text)
        self.assertNotIn('WHOLE FOODS', text)
        self.assertTrue(text.startswith(finance.UNTRUSTED))

    def test_an_unknown_account_is_an_error(self):
        _, error = run(self.registry.run('finance_transactions', dict(
            start='', end='', account='amex', query='', direction='all', limit=10, category='')))

        self.assertTrue(error)

    def test_a_card_payment_is_not_spending(self):
        text = self.call('spending_summary', start='', end='', account='', group='account')

        self.assertIn('out -$62.10, in $2,000.00', text)
        self.assertIn('2 transactions moving $500.00', text)

    def test_categories_come_from_the_merchant_code(self):
        text = self.call('spending_summary', start='', end='', account='', group='category')

        self.assertIn('- Dining: out -$20.00 (2)', text)
        self.assertIn('- Groceries: out -$42.10 (1)', text)
        self.assertIn('- Uncategorized: in $2,000.00 (1)', text)

    def test_a_category_summary_lists_merchants_still_to_sort(self):
        text = self.call('spending_summary', start='', end='', account='', group='category')

        self.assertIn('Merchants with no category yet (1,', text)
        self.assertIn('Acme Payroll ($2,000.00, 1)', text)

    def test_assigned_categories_win_and_cover_past_transactions(self):
        result = self.call('categorize', assignments=[
            {'merchant': 'ACME PAYROLL', 'category': 'Income'},
            {'merchant': 'Whole Foods #9', 'category': 'Groceries & household'},
        ])
        text = self.call('spending_summary', start='', end='', account='', group='category')

        self.assertIn('Saved 2', result)
        self.assertIn('- Income: in $2,000.00 (1)', text)
        self.assertIn('- Groceries & household: out -$42.10 (1)', text)
        self.assertNotIn('no category yet', text)

    def test_an_empty_category_removes_the_assignment(self):
        self.call('categorize', assignments=[{'merchant': 'Acme Payroll', 'category': 'Income'}])
        result = self.call('categorize', assignments=[{'merchant': 'Acme Payroll', 'category': ''}])

        self.assertIn('Removed: Acme Payroll', result)
        self.assertIn('No categories assigned yet', self.call('merchant_categories', query=''))

    def test_transactions_filter_by_category_and_show_it(self):
        self.call('categorize', assignments=[{'merchant': 'Blue Bottle', 'category': 'Coffee'}])
        text = self.call('finance_transactions', start='', end='', account='', query='', direction='all',
                         limit=50, category='coffee')

        self.assertIn('1 transactions', text)
        self.assertIn('· Coffee', text)

    def test_categories_are_listed_grouped(self):
        self.call('categorize', assignments=[{'merchant': 'Acme Payroll', 'category': 'Income'},
                                              {'merchant': 'Blue Bottle', 'category': 'Dining'}])

        self.assertEqual(self.call('merchant_categories', query=''), 'Dining: Blue Bottle\nIncome: Acme Payroll')
        self.assertEqual(self.call('merchant_categories', query='income'), 'Income: Acme Payroll')

    def test_too_many_at_once_is_refused(self):
        _, error = run(self.registry.run('categorize', {'assignments': [
            {'merchant': f'm{i}', 'category': 'Other'} for i in range(101)]}))

        self.assertTrue(error)

    def test_merchants_group_by_payee_or_cleaned_description(self):
        self.assertEqual(finance.merchant({'description': 'SQ *BLUE BOTTLE 10/14'}), 'Sq Blue Bottle')
        self.assertEqual(finance.merchant({'description': 'x', 'payee': "JOHN'S SHACK"}), "John's Shack")

    def test_a_period_before_the_synced_history_says_so(self):
        text = self.call('finance_transactions', start='2025-01-01', end='', account='', query='',
                         direction='all', limit=5, category='')

        self.assertIn('only goes back to', text)

    def test_sync_now_is_refused_soon_after_a_sync(self):
        run(self.store.save_state({'synced_at': datetime.now(timezone.utc), 'errors': []}))

        self.assertIn('Not synced again', self.call('sync_finances'))

    def test_the_tools_are_not_strict_and_have_no_unions(self):
        from tests.test_calendar import unions

        params = self.registry.params()

        self.assertEqual({t['strict'] for t in params}, {False})
        self.assertEqual(sum(unions(t['input_schema']) for t in params), 0)


if __name__ == '__main__':
    unittest.main()
