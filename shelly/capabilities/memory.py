''' long-term memory: tools to save, revise and forget notes, and the notes in every prompt

Every note is shown to Claude every turn rather than searched for. At the scale
of one person's notes that costs little, and Claude never has to guess that it
should look something up. If the notes grow past a few hundred, this is the
place to switch to retrieving only the relevant ones.
'''

import logging

from shelly.core.tools import Tool
from shelly.memory.notes import NoteError


log = logging.getLogger(__name__)

# Past this, showing every note every turn starts to cost real money; time to
# retrieve instead. A warning, not a cut-off: dropping notes silently is worse.
LARGE = 300

HEADER = ('Long-term notes you saved in earlier conversations, numbered, with the date '
          'each was last changed. They may be out of date; trust what the operator '
          'says now over a note, and fix the note.')

REMEMBER = '''Save a note to long-term memory, so it is in view in every future conversation, \
not just the recent messages. Use it when the operator shares something durable: a preference, \
a fact about their life or the people in it, a goal, a decision, a commitment, or a situation \
worth tracking over time. Do not save small talk, one-off requests, or anything already in the \
profile or the notes. One fact per note, written to make sense on its own later (say who, and \
give dates when timing matters). Mention briefly in your reply what you saved.'''

UPDATE = '''Rewrite a saved note, by its number, when it has changed or was wrong. Prefer this \
to saving a new note that contradicts an old one.'''

FORGET = '''Delete a saved note, by its number, when it is no longer true or the operator asks \
you to forget it.'''


def _schema(**properties):
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


TEXT = {'type': 'string', 'description': 'The note, one fact, written to stand on its own.'}
NUMBER = {'type': 'integer', 'description': 'The note number, as shown in the notes list.'}


def register(registry, notes):
    ''' give Claude the memory tools and put the notes in every prompt '''

    async def remember(text):
        try:
            n = await notes.add(text)
        except NoteError as error:
            return str(error)

        log.info('saved note %d', n)
        return f'Saved as note {n}.'

    async def update_memory(n, text):
        try:
            found = await notes.update(n, text)
        except NoteError as error:
            return str(error)

        return f'Note {n} updated.' if found else f'There is no note {n}.'

    async def forget(n):
        if await notes.remove(n):
            log.info('forgot note %d', n)
            return f'Note {n} deleted.'

        return f'There is no note {n}.'

    async def context():
        saved = await notes.all()

        if not saved:
            return None

        if len(saved) > LARGE:
            log.warning('%d notes are shown every turn; time to retrieve instead', len(saved))

        lines = [f'[{note["n"]}] ({note["updated_at"]:%Y-%m-%d}) {note["text"]}' for note in saved]

        return HEADER + '\n' + '\n'.join(lines)

    registry.add(
        Tool('remember', REMEMBER, _schema(text=TEXT), remember),
        Tool('update_memory', UPDATE, _schema(n=NUMBER, text=TEXT), update_memory),
        Tool('forget', FORGET, _schema(n=NUMBER), forget),
    )
    registry.add_context(context)
