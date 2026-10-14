''' the system prompt: who Shelly is, who it works for, and what time it is

In this order, as separate system blocks:

1. GUIDE -- how Shelly behaves -- and the operator's profile. The profile is read
   from a local file that is gitignored, because the repo is public and this is
   personal. Fixed for the life of the process.
2. Whatever the capabilities contribute this turn (saved notes, say). Changes
   only when something is saved.
3. The current local time -- the one part that changes every turn.

Ordered from most to least stable so that, once the prompt is long enough to be
worth caching, everything before the clock is a fixed prefix. The clock is taken
once per turn, not per request, so the tool rounds inside one turn agree on it.
'''

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


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
to do something no tool covers (set a reminder, send an email, look something up online), \
say plainly that you cannot do that yet. Never imply you did something you did not do.
- When you are unsure of a fact, say so instead of guessing. The operator would rather hear \
"I don't know" than a confident wrong answer.

Memory:
- You see only the recent part of the conversation, roughly the last ten messages from each \
side. Older messages are kept but not shown to you. If the operator refers to something \
you cannot see, say you do not have it in view and ask, rather than inventing it.
- Earlier messages in view may be hours or days old; the current time is given below, but \
the earlier messages carry no timestamps.'''

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


class Prompt:
    ''' builds the system parameter for each turn '''

    def __init__(self, profile=None, timezone=None, guide=GUIDE):
        self.timezone = timezone or ZoneInfo('UTC')
        stable = guide

        if profile:
            stable += f'\n\n{PROFILE_HEADER}\n{profile}'

        self.stable = stable

    def build(self, extra=(), now=None):
        ''' the system blocks for one turn: extra is capability context; now is for tests '''

        now = now or datetime.now(self.timezone)
        clock = (f'Current date and time for the operator: '
                 f'{now:%A %-d %B %Y, %H:%M} ({now.tzname()}, {self.timezone}).')

        return [
            {'type': 'text', 'text': self.stable},
            *({'type': 'text', 'text': text} for text in extra),
            {'type': 'text', 'text': clock},
        ]
