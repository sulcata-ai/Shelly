''' long-term notes: what Shelly has chosen to remember, across every conversation

Separate from the history on purpose. The history is what was said and slides
out of view; a note is a fact Shelly decided was worth keeping, written to stand
on its own, and it stays in view until it is changed or forgotten.

Notes are numbered 1, 2, 3... so Claude can name one in a tool call with a few
tokens rather than a 24-character ObjectId. Numbers come from an atomic counter,
not max+1: one turn can save two notes at once, and both would read the same max.
'''

from datetime import datetime, timezone

from pymongo import ASCENDING, ReturnDocument


COLLECTION = 'notes'
COUNTERS = 'counters'

# A note is one fact. Past this it is several, and should be saved as several.
MAX_NOTE = 500


class NoteError(ValueError):
    ''' a note that cannot be saved as given; the message is written for Claude '''


class Notes:

    def __init__(self, db):
        self.notes = db[COLLECTION]
        self.counters = db[COUNTERS]

    async def ensure_indexes(self):
        await self.notes.create_index([('n', ASCENDING)], unique=True)

    async def all(self):
        ''' every note, oldest first '''

        return await self.notes.find({}, {'_id': 0}).sort('n', ASCENDING).to_list()

    async def add(self, text):
        ''' save a note and return its number '''

        text = _checked(text)
        counter = await self.counters.find_one_and_update(
            {'_id': COLLECTION}, {'$inc': {'seq': 1}},
            upsert=True, return_document=ReturnDocument.AFTER)
        now = datetime.now(timezone.utc)

        await self.notes.insert_one({'n': counter['seq'], 'text': text, 'created_at': now, 'updated_at': now})

        return counter['seq']

    async def update(self, n, text):
        ''' rewrite note n; False when there is no such note '''

        result = await self.notes.update_one(
            {'n': n}, {'$set': {'text': _checked(text), 'updated_at': datetime.now(timezone.utc)}})

        return result.matched_count == 1

    async def remove(self, n):
        ''' delete note n; False when there is no such note '''

        result = await self.notes.delete_one({'n': n})

        return result.deleted_count == 1


def _checked(text):
    text = ' '.join(text.split())

    if not text:
        raise NoteError('A note cannot be empty.')

    if len(text) > MAX_NOTE:
        raise NoteError(f'A note is one fact, at most {MAX_NOTE} characters; this one is {len(text)}. '
                        'Split it into separate notes.')

    return text
