''' what each turn cost: tokens per request, summed per turn, priced, and kept

An estimate, not a bill. It prices the tokens the API reports with the table
below; the invoice is the authority. Two places it can be off: a refusal declined
before any output is reported but may not be billed (so this can over-count), and
a model missing from PRICES is not priced at all (cost None, logged).
'''

import logging
import time
from datetime import datetime, timezone

from pymongo import DESCENDING


log = logging.getLogger(__name__)

COLLECTION = 'turns'

# USD per million tokens: input, output, cache write (5-minute), cache read.
# Thinking is billed as output and is already inside output_tokens. The fallback
# targets are here because a declined request is re-run on them and billed at
# their rates. Update when prices or models change.
PRICES = {
    'claude-opus-5-5': (4.00, 20.00, 5.00, 0.20),
    'claude-opus-5':   (5.00, 25.00, 6.25, 0.50),
    'claude-opus-4-8': (5.00, 25.00, 6.25, 0.50),
}

TOKENS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def _attempts(usage, model):
    ''' (model, usage) for every attempt behind one response

    With the server-side fallback, usage.iterations lists each attempt with the
    model that ran it, and the top-level usage covers only the last one -- so the
    iterations are the source of truth when present, and the top level otherwise.
    '''

    iterations = getattr(usage, 'iterations', None) or []

    if iterations:
        return [(getattr(i, 'model', None) or model, i) for i in iterations]

    return [(model, usage)]


def _price(model, counts):
    rates = PRICES.get(model)

    if rates is None:
        return None

    return sum(n * rate for n, rate in zip(counts, rates)) / 1_000_000


class TurnUsage:
    ''' the running total for one turn, across every request in it '''

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.started = time.monotonic()
        self.requests = 0
        self.tokens = dict.fromkeys(TOKENS, 0)
        self.cost = 0.0
        self.unpriced = set()
        self.models = []
        self.tools = []
        self.outcome = None

    def add(self, response):
        ''' count one API response '''

        self.requests += 1

        for model, usage in _attempts(response.usage, response.model):
            counts = [getattr(usage, key, None) or 0 for key in TOKENS]

            for key, n in zip(TOKENS, counts):
                self.tokens[key] += n

            if model not in self.models:
                self.models.append(model)

            cost = _price(model, counts)

            if cost is None:
                self.unpriced.add(model)
            else:
                self.cost += cost

    def called(self, names):
        self.tools.extend(names)

    def document(self):
        ''' the record kept for this turn '''

        if self.unpriced:
            log.warning('no price for %s; cost for this turn is unknown', sorted(self.unpriced))

        return {
            'chat_id': self.chat_id,
            'created_at': datetime.now(timezone.utc),
            'outcome': self.outcome,
            'requests': self.requests,
            **self.tokens,
            'cost_usd': None if self.unpriced else round(self.cost, 6),
            'models': self.models,
            'tools': self.tools,
            'seconds': round(time.monotonic() - self.started, 2),
        }


class Ledger:
    ''' where turn records are kept: one document per turn '''

    def __init__(self, collection):
        self.collection = collection

    async def ensure_indexes(self):
        await self.collection.create_index([('created_at', DESCENDING)])

    async def record(self, doc):
        await self.collection.insert_one(doc)

    async def spent_since(self, since):
        ''' (turns, total cost) since a moment; for /status and the dashboard '''

        pipeline = [
            {'$match': {'created_at': {'$gte': since}}},
            {'$group': {'_id': None, 'turns': {'$sum': 1}, 'cost': {'$sum': '$cost_usd'}}},
        ]
        rows = await (await self.collection.aggregate(pipeline)).to_list()

        return (rows[0]['turns'], rows[0]['cost']) if rows else (0, 0.0)
