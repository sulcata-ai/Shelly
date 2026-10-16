''' what the new-mail check keeps between runs: where it is in Gmail, what waits, and the rules

Collection mail_watch, two documents. "state" is written only by the poller,
which runs one tick at a time: Gmail's history id it has read up to, and the
mail held back during quiet hours. "rules" is written only by the rule tool,
with $addToSet and $pull so two changes in one turn cannot undo each other.
Kept apart so a rule added while the poller runs is not overwritten by it.
'''

COLLECTION = 'mail_watch'
KINDS = ('always', 'never')


class MailWatch:

    def __init__(self, db):
        self.docs = db[COLLECTION]

    async def state(self):
        doc = await self.docs.find_one({'_id': 'state'})

        return doc or {'_id': 'state', 'history_id': None, 'pending': []}

    async def save_state(self, state):
        await self.docs.replace_one({'_id': 'state'}, {**state, '_id': 'state'}, upsert=True)

    async def rules(self):
        doc = await self.docs.find_one({'_id': 'rules'}) or {}

        return {kind: list(doc.get(kind, [])) for kind in KINDS}

    async def add_rule(self, kind, match):
        await self.docs.update_one({'_id': 'rules'}, {'$addToSet': {kind: match}}, upsert=True)

    async def remove_rule(self, kind, match):
        result = await self.docs.update_one({'_id': 'rules'}, {'$pull': {kind: match}})

        return result.modified_count > 0
