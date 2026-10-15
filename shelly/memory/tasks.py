''' the to-do list: tasks, their steps, and when to remind about each

One document per task in `tasks`:

    {n, chat_id, title, details, status, due, remind_at, repeat, done_log,
     steps: [{title, status, due, remind_at, repeat, done_at}],
     created_at, updated_at, done_at, v}

status is open | done | dropped (steps: open | done). Times are stored UTC.
A reminder belongs to the task or to one step: remind_at is when it next fires,
repeat is none | daily | weekdays | weekly. After it fires, a repeating reminder
moves to its next occurrence and a one-off is cleared; nothing fires for a done
step or a closed task.

A task with a repeat is a habit or a chore. Completing it finishes one
occurrence, not the task: the time goes on done_log, and due and remind_at move
on to the next occurrence. It stays open until it is dropped (or set done with
update). A due date left behind by missed days is read as the first occurrence
from today on, so a missed day never leaves the task looking overdue forever.

Writes are read-modify-replace guarded by a version number v. The reminder poller
and a tool call can touch the same task at the same moment, and without the guard
the slower one would silently undo the other.
'''

from datetime import date, datetime, time, timedelta, timezone

from pymongo import ASCENDING, DESCENDING, ReturnDocument


COLLECTION = 'tasks'
COUNTERS = 'counters'

STATUSES = ('open', 'done', 'dropped')
REPEATS = ('none', 'daily', 'weekdays', 'weekly')

MAX_TITLE = 200
MAX_DETAILS = 1500
MAX_STEPS = 25

# A date with no time, as a reminder: the start of the working day.
DEFAULT_REMIND = time(9, 0)

# How many completions of a repeating task are kept.
MAX_LOG = 100

# How many times to retry a write that lost a race before giving up.
ATTEMPTS = 5


class TaskError(ValueError):
    ''' a change that cannot be made as given; the message is written for Claude '''


def parse_when(text, tz, field):
    ''' an ISO date or datetime from Claude, read in the operator's zone, returned as UTC

    A bare date means 09:00 local -- for a due date and a reminder alike, the
    morning of that day. An explicit offset is respected.
    '''

    if text is None:
        return None

    text = text.strip()

    try:
        if len(text) == 10:
            moment = datetime.combine(date.fromisoformat(text), DEFAULT_REMIND, tz)
        else:
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=tz)
    except ValueError:
        raise TaskError(f'{field} {text!r} is not an ISO date or datetime like 2026-10-20 or 2026-10-20T09:00.')

    return moment.astimezone(timezone.utc)


def next_occurrence(at, repeat, tz, now):
    ''' the first time after now that a repeating reminder fires again, or None for one-offs

    Stepped on the local wall clock, so a 09:00 reminder stays at 09:00 across a
    daylight-saving change. Occurrences missed while Shelly was down are skipped,
    not delivered in a burst.
    '''

    if repeat in (None, 'none'):
        return None

    local = at.astimezone(tz)

    while True:
        local = (local.replace(tzinfo=None) + timedelta(days=7 if repeat == 'weekly' else 1)).replace(tzinfo=tz)

        if repeat == 'weekdays' and local.weekday() >= 5:
            continue

        if local.astimezone(timezone.utc) > now:
            return local.astimezone(timezone.utc)


def current_occurrence(at, repeat, tz, now):
    ''' the repeating due date at, or its first occurrence from the start of today (local) if at is earlier '''

    today = datetime.combine(now.astimezone(tz).date(), time(0), tz).astimezone(timezone.utc)

    if at >= today:
        return at

    return next_occurrence(at, repeat, tz, today - timedelta(microseconds=1))


def end_of_today(tz, now):
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)

    return datetime.combine(tomorrow, time(0), tz).astimezone(timezone.utc)


def complete_occurrence(task, tz, now):
    ''' mark one occurrence of a repeating task done: log it and move due and remind_at on

    The occurrence done is the current one: today's, or for a weekly task the one
    in the coming week. Done on a day with no occurrence (a weekday habit on a
    Saturday, or before its first day), it is logged and nothing moves. The
    occurrence's reminder is skipped if it has not fired yet: anything at or
    before its due time -- or, with no due date, before the end of today.
    '''

    task['done_log'] = (task.get('done_log') or [])[-(MAX_LOG - 1):] + [now]
    boundary = end_of_today(tz, now)

    if task.get('due') is not None:
        done = current_occurrence(task['due'], task['repeat'], tz, now)
        reach = boundary + timedelta(days=6 if task['repeat'] == 'weekly' else 0)

        if done >= reach:
            return

        task['due'] = next_occurrence(done, task['repeat'], tz, done)
        boundary = done

    if task.get('remind_at') is not None and task['remind_at'] <= boundary:
        task['remind_at'] = next_occurrence(task['remind_at'], task['repeat'], tz, boundary)


def _text(value, field, limit, required=False):
    value = (value or '').strip()

    if required and not value:
        raise TaskError(f'{field} cannot be empty.')

    if len(value) > limit:
        raise TaskError(f'{field} is {len(value)} characters; keep it under {limit}.')

    return value


def _repeat(value):
    value = value or 'none'

    if value not in REPEATS:
        raise TaskError(f'repeat must be one of {", ".join(REPEATS)}.')

    return value


def _remind(text, tz, now, field):
    at = parse_when(text, tz, field)

    if at is not None and at <= now:
        raise TaskError(f'{field} {text} is already past; pick a time after now.')

    return at


def build_steps(raw, tz, now, previous=()):
    ''' validated steps from the tool input; a step that was done stays done unless reopened '''

    if len(raw) > MAX_STEPS:
        raise TaskError(f'{len(raw)} steps is too many; keep it to {MAX_STEPS} and group the rest.')

    done_at = {s['title']: s.get('done_at') for s in previous if s.get('status') == 'done'}
    steps = []

    for i, step in enumerate(raw, 1):
        title = _text(step.get('title'), f'step {i} title', MAX_TITLE, required=True)
        status = step.get('status') or 'open'

        if status not in ('open', 'done'):
            raise TaskError(f'step {i} status must be open or done.')

        steps.append({
            'title': title,
            'status': status,
            'due': parse_when(step.get('due'), tz, f'step {i} due'),
            'remind_at': None if status == 'done' else _remind(step.get('remind_at'), tz, now, f'step {i} remind_at'),
            'repeat': _repeat(step.get('repeat')),
            'done_at': (done_at.get(title) or now) if status == 'done' else None,
        })

    return steps


class Tasks:

    def __init__(self, db, timezone):
        self.tasks = db[COLLECTION]
        self.counters = db[COUNTERS]
        self.tz = timezone

    async def ensure_indexes(self):
        await self.tasks.create_index([('n', ASCENDING)], unique=True)
        await self.tasks.create_index([('status', ASCENDING)])

    async def get(self, n):
        task = await self.tasks.find_one({'n': n}, {'_id': 0})

        if task is None:
            raise TaskError(f'There is no task {n}.')

        return task

    async def open(self):
        ''' every open task, oldest first '''

        return await self.tasks.find({'status': 'open'}, {'_id': 0}).sort('n', ASCENDING).to_list()

    async def closed(self, status, limit=15):
        ''' the most recently closed tasks with this status '''

        return await (self.tasks.find({'status': status}, {'_id': 0})
                      .sort('updated_at', DESCENDING).limit(limit).to_list())

    async def add(self, chat_id, title, details, due, remind_at, repeat, steps, now=None):
        now = now or datetime.now(timezone.utc)
        task = {
            'chat_id': chat_id,
            'title': _text(title, 'title', MAX_TITLE, required=True),
            'details': _text(details, 'details', MAX_DETAILS),
            'status': 'open',
            'due': parse_when(due, self.tz, 'due'),
            'remind_at': _remind(remind_at, self.tz, now, 'remind_at'),
            'repeat': _repeat(repeat),
            'steps': build_steps(steps or [], self.tz, now),
            'created_at': now,
            'updated_at': now,
            'done_at': None,
            'done_log': [],
            'v': 1,
        }

        counter = await self.counters.find_one_and_update(
            {'_id': COLLECTION}, {'$inc': {'seq': 1}}, upsert=True, return_document=ReturnDocument.AFTER)
        task['n'] = counter['seq']
        await self.tasks.insert_one(dict(task))
        task.pop('_id', None)

        return task

    async def change(self, n, edit, now=None):
        ''' apply edit(task, now) to task n under the version guard; returns the saved task '''

        for _ in range(ATTEMPTS):
            now = now or datetime.now(timezone.utc)
            task = await self.get(n)
            version = task['v']

            edit(task, now)
            task['updated_at'] = now
            task['v'] = version + 1

            result = await self.tasks.replace_one({'n': n, 'v': version}, task)

            if result.matched_count == 1:
                return task

        raise TaskError(f'Task {n} kept changing underneath this edit; try again.')

    async def update(self, n, title=None, details=None, due=None, remind_at=None, repeat=None, status=None):
        ''' change the task's own fields; None leaves a field alone, "" clears a date '''

        def edit(task, now):
            if title is not None:
                task['title'] = _text(title, 'title', MAX_TITLE, required=True)
            if details is not None:
                task['details'] = _text(details, 'details', MAX_DETAILS)
            if due is not None:
                task['due'] = parse_when(due, self.tz, 'due') if due else None
            if remind_at is not None:
                task['remind_at'] = _remind(remind_at, self.tz, now, 'remind_at') if remind_at else None
            if repeat is not None:
                task['repeat'] = _repeat(repeat)
            if status is not None:
                if status not in STATUSES:
                    raise TaskError(f'status must be one of {", ".join(STATUSES)}.')
                task['status'] = status
                task['done_at'] = now if status == 'done' else None

        return await self.change(n, edit)

    async def set_steps(self, n, steps):
        ''' replace the task's steps with a revised list '''

        def edit(task, now):
            task['steps'] = build_steps(steps, self.tz, now, previous=task['steps'])

        return await self.change(n, edit)

    async def complete(self, n, step=None, now=None):
        ''' mark a step (1-based) or the whole task done; finishing the last open step finishes the task

        The whole of a repeating task is never finished this way: one occurrence is.
        '''

        def edit(task, now):
            if step is None:
                if task['status'] == 'open' and task.get('repeat', 'none') != 'none':
                    complete_occurrence(task, self.tz, now)
                else:
                    task['status'], task['done_at'] = 'done', now
                return

            if not 1 <= step <= len(task['steps']):
                raise TaskError(f'Task {n} has no step {step}; it has {len(task["steps"])}.')

            target = task['steps'][step - 1]
            target.update(status='done', done_at=now, remind_at=None)

            if all(s['status'] == 'done' for s in task['steps']):
                task['status'], task['done_at'] = 'done', now

        return await self.change(n, edit, now=now)

    async def fire_due(self, now=None):
        ''' every reminder due by now, as (task, step index or None, was_late); advances or clears each

        A reminder more than LATE behind is still delivered, once, and flagged
        late -- the operator should hear about it, and know it is late.
        '''

        now = now or datetime.now(timezone.utc)
        fired = []

        for task in await self.open():
            due_here = [(None, task)] if _due(task, now) else []
            due_here += [(i, s) for i, s in enumerate(task['steps']) if s['status'] == 'open' and _due(s, now)]

            if not due_here:
                continue

            late = {i: now - item['remind_at'] > LATE for i, item in due_here}

            def edit(task, now, due_here=due_here):
                for i, _ in due_here:
                    item = task if i is None else task['steps'][i]
                    if item.get('remind_at') is not None and item['remind_at'] <= now:
                        item['remind_at'] = next_occurrence(item['remind_at'], item.get('repeat'), self.tz, now)

            try:
                saved = await self.change(task['n'], edit, now=now)
            except TaskError:
                continue  # it changed under us; whatever is still due fires next tick

            fired += [(saved, i, late[i]) for i, _ in due_here]

        return fired


# A reminder this far behind is flagged as late when it is finally sent.
LATE = timedelta(minutes=10)


def _due(item, now):
    return item.get('remind_at') is not None and item['remind_at'] <= now
