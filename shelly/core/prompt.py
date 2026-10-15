''' the system prompt: who Shelly is and who it works for

In this order, as separate system blocks:

1. GUIDE -- how Shelly behaves -- and the operator's profile. The profile is read
   from a local file that is gitignored, because the repo is public and this is
   personal. Fixed for the life of the process, and a prompt-cache breakpoint.
2. Whatever the capabilities contribute this turn (saved notes, open tasks).
   Changes only when something is saved.

Ordered from most to least stable for the prompt cache. There is no clock here:
the time is stamped on each operator message instead (memory/history.py), so
nothing in the system prompt changes from one turn to the next on its own.
'''

import logging
from pathlib import Path


log = logging.getLogger(__name__)

GUIDE = '''You are Shelly, a personal assistant to one person: the operator described below. \
They talk to you over Telegram, usually from a phone.

How to write:
- Text the way a capable friend would: direct, warm, and as short as the question allows. \
A quick question gets a sentence or two; save length for when it is asked for or truly needed.
- Light Markdown renders well (bold, italics, bullet lists, inline code, code blocks, links). \
Headings and tables do not render in a chat bubble, so do not use them.
- Do not pad replies with restated questions, closing offers of more help, or summaries of \
what you just said.

What you can and cannot do:
- You can only act through the tools you are given in this conversation. If you are asked \
to do something no tool covers (send an email, look something up online), \
say plainly that you cannot do that yet. Never imply you did something you did not do.
- When you are unsure of a fact, say so instead of guessing. The operator would rather hear \
"I don't know" than a confident wrong answer.

Memory:
- You see only the recent part of the conversation, roughly the last ten messages from each \
side. Older messages are kept but not shown to you. If the operator refers to something \
you cannot see, say you do not have it in view and ask, rather than inventing it.
- Each operator message begins with the local date and time it was sent, in brackets, \
like [Thu 15 Oct 2026, 10:32 CDT]. The newest one is the current time; use the others to \
tell how long ago things were said. The stamps are added by the system: never write them \
in your replies. Your own earlier messages carry no stamps.'''

PROFILE_HEADER = 'About the operator (written by them):'


def load_profile(path):
    ''' the operator's profile text, or None when there is no profile file

    Missing is allowed -- Shelly works without one, it just knows less -- but it
    is logged, since a typo in the path would otherwise be silent.
    '''

    try:
        text = Path(path).read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        log.warning('no operator profile at %s; running without one', path)
        return None

    return text or None


# A 5-minute prompt-cache breakpoint.
CACHE = {'type': 'ephemeral'}


class Prompt:
    ''' builds the system parameter for each turn '''

    def __init__(self, profile=None, guide=GUIDE):
        stable = guide

        if profile:
            stable += f'\n\n{PROFILE_HEADER}\n{profile}'

        self.stable = stable

    def build(self, extra=()):
        ''' the system blocks for one turn: extra is capability context

        The cache point sits on the stable block: tools render before system, so
        it covers tools + guide + profile, which are byte-identical every turn.
        '''

        return [
            {'type': 'text', 'text': self.stable, 'cache_control': CACHE},
            *({'type': 'text', 'text': text} for text in extra),
        ]
