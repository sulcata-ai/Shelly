''' stand-ins for Claude and the history store, so the core runs with no network '''

import asyncio
from types import SimpleNamespace

from shelly.memory.history import build_window


def text(value):
    return SimpleNamespace(type='text', text=value)


def thinking():
    return SimpleNamespace(type='thinking', thinking='', signature='sig')


def tool_use(name, tool_input, id='tu_1'):
    return SimpleNamespace(type='tool_use', id=id, name=name, input=tool_input)


def response(*content, stop='end_turn', category=None):
    details = SimpleNamespace(category=category) if stop == 'refusal' else None
    return SimpleNamespace(content=list(content), stop_reason=stop, stop_details=details)


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

    async def window(self, chat_id):
        return build_window(d for d in self.docs if d['chat_id'] == chat_id)
