''' the to-do list: tools to plan and track tasks, the open list in every prompt, and reminders

How Shelly should plan a task lives in ADD_TASK, the add_task description:
gather what it needs, break big work into steps, schedule them in turn, set
reminders by judgment, and ask only when it truly cannot infer something.

Reminders are written from a template, not by Claude: they are sent on a timer
with nobody waiting, so they should be instant, free and never fail for a model
reason. Each one is also recorded in the conversation, so a reply like "done"
makes sense to Shelly on the next turn.
'''

import logging
from datetime import datetime, timezone

from shelly.core.context import current_chat
from shelly.core.tools import Tool
from shelly.memory.tasks import REPEATS, TaskError, current_occurrence


log = logging.getLogger(__name__)

ADD_TASK = '''Add a task to the operator's to-do list, planned and scheduled.

Before calling this, make sure the task is well defined: what it is, what "done" looks \
like, and when it is due if there is a deadline. Fill gaps from what you already know \
(the conversation, the profile, your notes) and use judgment. Ask the operator only when \
something material cannot be reasonably inferred -- a hard deadline you cannot guess, or a \
choice only they can make -- and then ask everything you need in one short message.

Break a task that takes more than one sitting into concrete, ordered steps, each small \
enough to finish in one sitting and starting with a verb. Schedule them in turn: give \
each step a due date, spaced realistically from today to the deadline, leaving slack \
before the deadline. A small single-sitting task needs no steps.

Set reminders as part of planning, by judgment: usually on the morning a step is due \
(09:00 local unless the operator's habits suggest otherwise), and on the task itself \
for the deadline when it matters. Use repeat for habits and recurring chores. Do not \
remind about every trivial thing.

A habit or recurring chore is one task with repeat set (on its due date and reminder), \
not steps: completing it counts one occurrence and moves it to the next, and it stays \
on the list until dropped. Its line in the open-tasks list shows when it was recently \
done, which is how you can tell a streak.

Times are the operator's local time, as ISO: 2026-10-20 (a date means 09:00) or \
2026-10-20T18:30. After adding, tell the operator the plan in a few lines: the steps \
with their dates and when you will remind them.'''

UPDATE_TASK = '''Change a task's own fields: title, details, due date, reminder, repeat, or \
status (open, done, dropped -- dropped means no longer wanted). Pass null for every \
field you are not changing, and an empty string to clear a date or reminder. To change \
the steps, use set_steps.'''

SET_STEPS = '''Replace a task's steps with a revised list, in order: to add, remove, \
reorder, rename or reschedule steps. Give the whole list, including steps that are \
already done (with status "done"), copying what is unchanged from the open-tasks list.'''

COMPLETE = '''Mark a step of a task done (step is its number in the task, from 1), or \
the whole task done (step null). Finishing the last open step finishes the task. Use \
this when the operator says they did something, even in passing. For a repeating task \
(step null), this records today's occurrence as done and moves it to the next one; the \
task stays open. To stop a repeating task, set its status to dropped with update_task.'''

LIST_CLOSED = '''The most recently finished (done) or dropped tasks, for when the \
operator asks about past tasks. Open tasks are already in your prompt.'''

# How many recent completions of a repeating task the open list shows.
RECENT = 10

HEADER = ('Open tasks on the operator\'s to-do list (numbers are for the task tools; '
          'times are the operator\'s local time; each line shows due date and next reminder):')


def _nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


DATE = {'type': 'string', 'description': 'ISO local date or datetime, e.g. 2026-10-20 or 2026-10-20T18:30'}
REPEAT = {'type': 'string', 'enum': list(REPEATS)}


def _object(**properties):
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


STEP = _object(
    title={'type': 'string'},
    status={'type': 'string', 'enum': ['open', 'done']},
    due=_nullable(DATE),
    remind_at=_nullable(DATE),
    repeat=REPEAT,
)

NEW_STEP = _object(title={'type': 'string'}, due=_nullable(DATE), remind_at=_nullable(DATE), repeat=REPEAT)


def register(registry, tasks):
    ''' give Claude the to-do tools, the open list every turn, and reminders on a timer '''

    tz = tasks.tz

    def when(moment, fmt='%a %d %b %H:%M'):
        return moment.astimezone(tz).strftime(fmt)

    def schedule(item, now):
        parts = []
        repeating = item.get('repeat') not in (None, 'none')

        if item.get('due'):
            due = current_occurrence(item['due'], item['repeat'], tz, now) if repeating else item['due']
            parts.append(f'{"next " if repeating else ""}due {when(due)}')

        if item.get('remind_at'):
            repeat = item.get('repeat')
            parts.append(f'reminder {when(item["remind_at"])}' + (f', {repeat}' if repeat not in (None, 'none') else ''))

        return f' -- {" · ".join(parts)}' if parts else ''

    def render(task, now=None):
        now = now or datetime.now(timezone.utc)
        status = '' if task['status'] == 'open' else f' [{task["status"]}]'
        lines = [f'#{task["n"]} {task["title"]}{status}{schedule(task, now)}']

        if task.get('repeat') not in (None, 'none'):
            history = task.get('done_log') or []
            recent = ', '.join(when(d, '%a %d %b') for d in history[-RECENT:]) or 'never yet'
            lines.append(f'   done {len(history)} times; recently: {recent}')

        if task.get('details'):
            lines.append(f'   details: {task["details"]}')

        for i, step in enumerate(task['steps'], 1):
            mark = 'x' if step['status'] == 'done' else ' '
            lines.append(f'   {i}. [{mark}] {step["title"]}{schedule(step, now) if step["status"] == "open" else ""}')

        return '\n'.join(lines)

    async def attempt(action):
        try:
            return await action()
        except TaskError as error:
            return str(error)

    async def add_task(title, details, due, remind_at, repeat, steps):
        async def go():
            task = await tasks.add(current_chat.get(), title, details, due, remind_at, repeat, steps)
            log.info('added task %d with %d steps', task['n'], len(task['steps']))
            return 'Added:\n' + render(task)

        return await attempt(go)

    async def update_task(n, title, details, due, remind_at, repeat, status):
        async def go():
            task = await tasks.update(n, title, details, due, remind_at, repeat, status)
            return 'Updated:\n' + render(task)

        return await attempt(go)

    async def set_steps(n, steps):
        async def go():
            return 'Steps updated:\n' + render(await tasks.set_steps(n, steps))

        return await attempt(go)

    async def complete_task(n, step):
        async def go():
            task = await tasks.complete(n, step)

            if task['status'] == 'done':
                done = 'Task done.'
            elif step is not None:
                done = f'Step {step} done.'
            else:
                done = 'Done for this time; it repeats.'

            return f'{done}\n{render(task)}'

        return await attempt(go)

    async def list_closed_tasks(status):
        closed = await tasks.closed(status)

        if not closed:
            return f'No {status} tasks.'

        return '\n'.join(f'{render(t)} (closed {when(t["updated_at"], "%d %b")})' for t in closed)

    async def context():
        open_tasks = await tasks.open()

        if not open_tasks:
            return None

        return HEADER + '\n' + '\n'.join(render(t) for t in open_tasks)

    def reminder(task, step, late, now):
        if step is None:
            subject = f'**{task["title"]}** (task #{task["n"]}'
            due = task.get('due')
            if due and task.get('repeat') not in (None, 'none'):
                due = current_occurrence(due, task['repeat'], tz, now)
            following = next((s['title'] for s in task['steps'] if s['status'] == 'open'), None)
        else:
            item = task['steps'][step]
            subject = f'**{item["title"]}** (step {step + 1} of {len(task["steps"])} of #{task["n"]} {task["title"]}'
            due = item.get('due')
            following = None

        text = f'⏰ Reminder: {subject}' + (f', due {when(due)})' if due else ')')

        if following:
            text += f'\nNext step: {following}'

        if late:
            text += '\n(Sent late: I was offline when this was due.)'

        return text

    async def poll(now):
        messages = []

        for task, step, late in await tasks.fire_due(now):
            if task.get('chat_id') is None:
                log.warning('task %d has no chat to remind in', task['n'])
                continue

            messages.append((task['chat_id'], reminder(task, step, late, now)))

        return messages

    n = {'type': 'integer', 'description': 'The task number, as shown in the open-tasks list.'}

    registry.add(
        Tool('add_task', ADD_TASK, _object(
            title={'type': 'string'},
            details={'type': 'string', 'description': 'What done looks like, constraints, context. May be empty.'},
            due=_nullable(DATE),
            remind_at=_nullable(DATE),
            repeat=REPEAT,
            steps={'type': 'array', 'items': NEW_STEP},
        ), add_task),
        Tool('update_task', UPDATE_TASK, _object(
            n=n,
            title=_nullable({'type': 'string'}),
            details=_nullable({'type': 'string'}),
            due=_nullable({'type': 'string'}),
            remind_at=_nullable({'type': 'string'}),
            repeat=_nullable(REPEAT),
            status=_nullable({'type': 'string', 'enum': ['open', 'done', 'dropped']}),
        ), update_task),
        Tool('set_steps', SET_STEPS, _object(n=n, steps={'type': 'array', 'items': STEP}), set_steps),
        Tool('complete_task', COMPLETE, _object(
            n=n, step=_nullable({'type': 'integer', 'description': 'Step number from 1, or null for the whole task.'}),
        ), complete_task),
        Tool('list_closed_tasks', LIST_CLOSED, _object(
            status={'type': 'string', 'enum': ['done', 'dropped']},
        ), list_closed_tasks),
    )
    registry.add_context(context)
    registry.add_poller(poll)
