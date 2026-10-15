''' stand-ins for Claude and the history store, so the core runs with no network '''

import asyncio
import copy
from types import SimpleNamespace

from shelly.memory.history import build_window


def text(value):
    return SimpleNamespace(type='text', text=value)


def thinking():
    return SimpleNamespace(type='thinking', thinking='', signature='sig')


def tool_use(name, tool_input, id='tu_1'):
    return SimpleNamespace(type='tool_use', id=id, name=name, input=tool_input)


def usage(input_tokens=100, output_tokens=10, cache_read=0, cache_write=0, iterations=None, model=None):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
                           iterations=iterations, model=model)


def response(*content, stop='end_turn', category=None, model='claude-opus-5-5', use=None):
    details = SimpleNamespace(category=category) if stop == 'refusal' else None
    return SimpleNamespace(content=list(content), stop_reason=stop, stop_details=details,
                           model=model, usage=use or usage())


class FakeClaude:
    ''' answers with the scripted responses in order and records every request '''

    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.error = error
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        # a snapshot: the agent keeps appending to the same list
        self.requests.append(dict(request, messages=list(request['messages'])))

        # yield, as a real request would, so concurrent turns can interleave
        await asyncio.sleep(0)

        if self.error:
            raise self.error

        return self.responses.pop(0)


class FakeHistory:
    ''' History's interface over a plain list '''

    def __init__(self):
        self.docs = []

    async def add(self, chat_id, role, text):
        self.docs.append({'chat_id': chat_id, 'role': role, 'text': text})

    async def mark_reset(self, chat_id):
        self.docs.append({'chat_id': chat_id, 'role': 'reset', 'text': ''})

    async def window(self, chat_id):
        return build_window(d for d in self.docs if d['chat_id'] == chat_id)


class _Cursor:

    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self):
        return self.docs


class FakeCollection:
    ''' the slice of an async pymongo collection the notes store uses '''

    def __init__(self):
        self.docs = []

    async def create_index(self, keys, unique=False):
        pass

    def _match(self, query):
        return [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]

    def find(self, query, projection=None):
        hidden = {k for k, v in (projection or {}).items() if not v}
        return _Cursor([{k: v for k, v in d.items() if k not in hidden} for d in self._match(query)])

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))

    async def find_one(self, query, projection=None):
        found = self.find(query, projection).docs
        return copy.deepcopy(found[0]) if found else None

    async def replace_one(self, query, doc):
        found = self._match(query)[:1]

        for old in found:
            self.docs[self.docs.index(old)] = copy.deepcopy(doc)

        return SimpleNamespace(matched_count=len(found))

    async def find_one_and_update(self, query, update, upsert=False, return_document=None):
        found = self._match(query)

        if not found and upsert:
            found = [dict(query)]
            self.docs.append(found[0])

        doc = found[0]

        for key, step in update.get('$inc', {}).items():
            doc[key] = doc.get(key, 0) + step

        return dict(doc)

    async def update_one(self, query, update):
        found = self._match(query)

        for doc in found[:1]:
            doc.update(update.get('$set', {}))

        return SimpleNamespace(matched_count=len(found[:1]))

    async def delete_one(self, query):
        found = self._match(query)[:1]

        for doc in found:
            self.docs.remove(doc)

        return SimpleNamespace(deleted_count=len(found))


class FakeDb(dict):
    ''' db[name] hands out one FakeCollection per name '''

    def __missing__(self, name):
        self[name] = FakeCollection()
        return self[name]
