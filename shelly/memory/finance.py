''' the operator's bank and card data as last synced from SimpleFIN

Three collections written only by the sync (capabilities/finance.py), and one by
the categorize tool:

- fin_accounts: one document per account, _id the SimpleFIN account id:
  {name, institution, currency, balance, available, balance_at, holdings, synced_at};
  holdings are investment positions, [{symbol, description, shares, value, cost}].
- fin_transactions: one per transaction, _id "<account id>:<transaction id>":
  {account_id, account, currency, amount, at, posted_at, description, payee,
  memo, mcc, pending}. payee, memo and mcc (the card network's merchant
  category code) are there when the bank gives them; empty otherwise.
  amount is the bank's decimal string (negative is money out), kept exact;
  at is when it happened (transacted_at, else posted), UTC.
- fin_sync, _id "state": {synced_at, history_from, errors, failure} -- when the
  last sync succeeded, how far back the data goes, and what was already told.
- fin_categories: one per merchant, _id the merchant's lowercased name as the
  summaries show it: {category, updated_at}. Shelly assigns them; they apply to
  past and future transactions alike.

A pending transaction usually comes back under a new id once it posts, so each
sync removes the pending ones it covers before saving what the bank says now.
'''

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from pymongo import ReplaceOne


ACCOUNTS = 'fin_accounts'
TRANSACTIONS = 'fin_transactions'
SYNC = 'fin_sync'
CATEGORIES = 'fin_categories'


def _when(epoch):
    try:
        epoch = int(epoch or 0)
    except (TypeError, ValueError):
        return None

    return datetime.fromtimestamp(epoch, timezone.utc) if epoch > 0 else None


def amount(text):
    ''' the bank's numeric string as a Decimal; anything unreadable is 0 '''

    try:
        return Decimal(str(text))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def account_doc(raw, institution, now):
    return {
        '_id': str(raw['id']),
        'name': str(raw.get('name') or 'Account'),
        'institution': institution or '',
        'currency': str(raw.get('currency') or 'USD'),
        'balance': str(raw.get('balance') or '0'),
        'available': raw.get('available-balance'),
        'balance_at': _when(raw.get('balance-date')),
        'holdings': [{'symbol': str(h.get('symbol') or ''), 'description': str(h.get('description') or ''),
                      'shares': str(h.get('shares') or ''), 'value': str(h.get('market_value') or '0'),
                      'cost': str(h.get('cost_basis') or '')}
                     for h in raw.get('holdings') or [] if isinstance(h, dict)],
        'synced_at': now,
    }


def transaction_doc(raw, account):
    ''' None for a transaction with no usable time '''

    posted = _when(raw.get('posted'))
    at = _when(raw.get('transacted_at')) or posted

    if at is None:
        return None

    pending = bool(raw.get('pending')) or posted is None
    def text(key):
        return ' '.join(str(raw.get(key) or '').split())

    description = text('description')

    return {
        '_id': f'{account["_id"]}:{raw["id"]}',
        'account_id': account['_id'],
        'account': account['name'],
        'currency': account['currency'],
        'amount': str(raw.get('amount') or '0'),
        'at': at,
        'posted_at': posted,
        'description': description or '(no description)',
        'payee': text('payee'),
        'memo': text('memo'),
        'mcc': text('mcc'),
        'pending': pending,
    }


class Finance:

    def __init__(self, db):
        self.accounts_docs = db[ACCOUNTS]
        self.transactions_docs = db[TRANSACTIONS]
        self.sync_docs = db[SYNC]
        self.category_docs = db[CATEGORIES]

    async def ensure_indexes(self):
        await self.transactions_docs.create_index([('at', -1)])
        await self.transactions_docs.create_index([('account_id', 1), ('at', -1)])

    async def state(self):
        doc = await self.sync_docs.find_one({'_id': 'state'})

        return doc or {'_id': 'state', 'synced_at': None, 'errors': []}

    async def save_state(self, state):
        await self.sync_docs.replace_one({'_id': 'state'}, {**state, '_id': 'state'}, upsert=True)

    async def save(self, account_set, institutions, start, now):
        ''' store one sync's accounts and transactions; returns (accounts, transactions) saved '''

        saved_accounts, saved_transactions = 0, 0

        for raw in account_set.get('accounts') or []:
            if not isinstance(raw, dict) or 'id' not in raw:
                continue

            institution = (institutions.get(raw.get('conn_id'))
                           or (raw.get('org') or {}).get('name') or raw.get('conn_name') or '')
            account = account_doc(raw, institution, now)
            await self.accounts_docs.replace_one({'_id': account['_id']}, account, upsert=True)
            saved_accounts += 1

            docs = [d for d in (transaction_doc(t, account) for t in raw.get('transactions') or []
                                if isinstance(t, dict) and 'id' in t) if d]
            # Pending ones the bank no longer reports (posted under a new id, or
            # dropped) must not linger beside what they became.
            await self.transactions_docs.delete_many(
                {'account_id': account['_id'], 'pending': True, 'at': {'$gte': start}})

            if docs:
                await self.transactions_docs.bulk_write(
                    [ReplaceOne({'_id': d['_id']}, d, upsert=True) for d in docs], ordered=False)

            saved_transactions += len(docs)

        return saved_accounts, saved_transactions

    async def accounts(self):
        return await self.accounts_docs.find({}).sort('name', 1).to_list()

    async def transactions(self, start, end, account_ids=None):
        ''' transactions with start <= at < end, newest first '''

        query = {'at': {'$gte': start, '$lt': end}}

        if account_ids:
            query['account_id'] = {'$in': list(account_ids)}

        return await self.transactions_docs.find(query).sort('at', -1).to_list()

    async def categories(self):
        ''' merchant key -> category '''

        return {d['_id']: d['category'] for d in await self.category_docs.find({}).to_list()}

    async def set_category(self, key, category, now):
        await self.category_docs.replace_one(
            {'_id': key}, {'_id': key, 'category': category, 'updated_at': now}, upsert=True)

    async def remove_category(self, key):
        return (await self.category_docs.delete_one({'_id': key})).deleted_count > 0
