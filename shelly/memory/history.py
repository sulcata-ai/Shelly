''' the conversation store, and the window of it that Claude sees

Every message is kept; only the window is limited. What is stored is the final
text of each side -- never thinking blocks, tool calls or tool results -- so a
replayed earlier turn is plain text, and sliding the window forward never edits
anything the model holds to the letter.
'''

from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient


# How many stored messages the window is cut from: roughly the last ten from each
# side. Counted in messages rather than tokens because messages here are short
# chat lines; revisit if long pastes become common.
HISTORY_LIMIT = 20

ROLES = ('user', 'assistant')

# A /reset leaves a marker in the history instead of deleting anything: the window
# starts after the newest marker, and everything before it is still on record.
RESET = 'reset'

COLLECTION = 'messages'


def build_window(docs):
    ''' turn stored messages, oldest first, into the API's messages list

    Two repairs, both needed because the store records what happened rather than
    what the API accepts, plus /reset:

    - A reset marker empties the window: only what came after it is shown.

    - The window must open on a user message. Cutting at HISTORY_LIMIT can land
      on a reply, so leading assistant messages are dropped.
    - Roles must alternate. A turn that failed leaves a user message with no
      reply, and the next one sits beside it; consecutive messages from one side
      are joined into one, in order, rather than dropping what was said.
    '''

    messages = []

    for doc in docs:
        role, text = doc.get('role'), doc.get('text')

        if role == RESET:
            messages = []
            continue

        if role not in ROLES or not text:
            continue

        if not messages and role == 'assistant':
            continue

        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += '\n\n' + text
        else:
            messages.append({'role': role, 'content': text})

    return messages


class History:
    ''' one chat store over a MongoDB collection

    Not safe for two turns of the same chat at once: the caller (the agent)
    serializes turns per chat, so history order is the order things happened.
    '''

    def __init__(self, collection, limit=HISTORY_LIMIT):
        self.collection = collection
        self.limit = limit

    async def ensure_indexes(self):
        ''' the one index the window query needs; cheap to repeat at startup '''

        await self.collection.create_index(
            [('chat_id', ASCENDING), ('created_at', DESCENDING), ('_id', DESCENDING)])

    async def add(self, chat_id, role, text):
        ''' record one message from either side '''

        if role not in ROLES:
            raise ValueError(f'role must be one of {ROLES}, not {role!r}')

        await self.collection.insert_one({
            'chat_id': chat_id,
            'role': role,
            'text': text,
            'created_at': datetime.now(timezone.utc),
        })

    async def mark_reset(self, chat_id):
        ''' start the chat's window afresh from here, deleting nothing '''

        await self.collection.insert_one({
            'chat_id': chat_id,
            'role': RESET,
            'text': '',
            'created_at': datetime.now(timezone.utc),
        })

    async def window(self, chat_id):
        ''' the last `limit` messages of a chat, as the API's messages list

        Newest first out of the index, then reversed. _id breaks ties between
        messages stamped in the same millisecond -- Mongo keeps milliseconds only,
        and a user message and a fast failure reply can share one.
        '''

        cursor = (self.collection.find({'chat_id': chat_id}, {'role': 1, 'text': 1})
                  .sort([('created_at', DESCENDING), ('_id', DESCENDING)])
                  .limit(self.limit))
        docs = await cursor.to_list()

        return build_window(reversed(docs))


def connect(cfg):
    ''' the client and the History over it; the caller closes the client on shutdown '''

    client = AsyncMongoClient(cfg.mongodb_uri, tz_aware=True)

    return client, History(client[cfg.mongodb_db][COLLECTION])
