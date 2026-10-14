''' /status: is Shelly well, and what has it cost '''

import time
from datetime import datetime, timedelta


def _duration(seconds):
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    if days:
        return f'{days}d {hours}h'

    return f'{hours}h {minutes}m' if hours else f'{minutes}m'


def _money(amount):
    return '$0' if not amount else f'${amount:.2f}' if amount >= 0.1 else f'${amount:.3f}'


class Status:
    ''' builds the /status reply from the live pieces

    Every number is read fresh, so a /status that answers at all has just
    proven the database is reachable; one that cannot reach it says so instead
    of failing silently.
    '''

    def __init__(self, agent, notes, ledger, timezone):
        self.agent = agent
        self.notes = notes
        self.ledger = ledger
        self.timezone = timezone
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone)

    async def report(self, chat_id, now=None):
        now = now or datetime.now(self.timezone)
        uptime = _duration(time.monotonic() - self.started)
        lines = [
            f'**Up** {uptime} (since {self.started_at:%a %H:%M})',
            f'**Model** {self.agent.model}, effort {self.agent.effort}',
        ]

        try:
            in_view = len(await self.agent.history.window(chat_id))
            notes = len(await self.notes.all())
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_turns, today = await self.ledger.spent_since(midnight)
            _, week = await self.ledger.spent_since(midnight - timedelta(days=6))
        except Exception as error:
            lines.append(f'**Database** unreachable: {type(error).__name__}')
            return '\n'.join(lines)

        lines += [
            f'**In view** {in_view} message{"s" if in_view != 1 else ""} (since last /reset)',
            f'**Notes** {notes}',
            f'**Spend** today {_money(today)} over {today_turns} turn{"s" if today_turns != 1 else ""}, '
            f'last 7 days {_money(week)}',
        ]

        return '\n'.join(lines)
