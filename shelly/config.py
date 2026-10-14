''' everything Shelly reads from the environment, read once and checked at startup '''

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


class ConfigError(Exception):
    ''' the environment cannot run Shelly; the message lists every problem at once '''


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_chat_ids: frozenset
    anthropic_api_key: str
    model: str
    effort: str
    mongodb_uri: str
    mongodb_db: str
    dashboard_url: str | None
    timezone: ZoneInfo
    profile_path: str

    def __repr__(self):
        # Never the secrets: this ends up in logs and tracebacks.
        return (f'Config(model={self.model!r}, effort={self.effort!r}, '
                f'mongodb_db={self.mongodb_db!r}, timezone={str(self.timezone)!r}, '
                f'allowed_chats={len(self.allowed_chat_ids)})')


def _chat_ids(raw):
    ''' a comma-separated list of Telegram chat ids; group ids are negative '''

    ids = set()

    for part in raw.split(','):
        part = part.strip()

        if part:
            ids.add(int(part))

    return frozenset(ids)


def load(env=None):
    ''' build the Config from env (default: os.environ after reading .env)

    Every problem is collected before raising, so a fresh setup is fixed in one
    pass rather than one restart per missing variable. An empty allowlist is a
    problem, not "allow everyone": a Telegram bot is publicly discoverable, and
    failing open would hand it to anyone who finds it.
    '''

    if env is None:
        load_dotenv()
        env = os.environ

    problems = []

    def required(name):
        value = (env.get(name) or '').strip()

        if not value:
            problems.append(f'{name} is not set')

        return value

    telegram_token = required('TELEGRAM_BOT_TOKEN')
    anthropic_api_key = required('ANTHROPIC_API_KEY')
    model = required('ANTHROPIC_MODEL')
    mongodb_uri = required('MONGODB_URI')
    raw_ids = required('TELEGRAM_ALLOWED_CHAT_IDS')

    allowed_chat_ids = frozenset()

    if raw_ids:
        try:
            allowed_chat_ids = _chat_ids(raw_ids)
        except ValueError:
            problems.append('TELEGRAM_ALLOWED_CHAT_IDS must be comma-separated integers')
        else:
            if not allowed_chat_ids:
                problems.append('TELEGRAM_ALLOWED_CHAT_IDS lists no chat ids')

    # Set explicitly rather than left to the API: claude-opus-5-5 defaults to
    # medium, other models to high, and a model swap should not quietly change it.
    effort = (env.get('ANTHROPIC_EFFORT') or 'medium').strip().lower()

    if effort not in EFFORTS:
        problems.append(f'ANTHROPIC_EFFORT must be one of {", ".join(EFFORTS)}')

    # The operator's own zone, not the Jetson's: replies say "tonight" and
    # "tomorrow" from where the operator is.
    timezone = None
    zone = (env.get('SHELLY_TIMEZONE') or 'America/Chicago').strip()

    try:
        timezone = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f'SHELLY_TIMEZONE {zone!r} is not an IANA time zone')

    if problems:
        raise ConfigError('; '.join(problems))

    return Config(
        telegram_token=telegram_token,
        allowed_chat_ids=allowed_chat_ids,
        anthropic_api_key=anthropic_api_key,
        model=model,
        effort=effort,
        mongodb_uri=mongodb_uri,
        mongodb_db=(env.get('MONGODB_DB') or 'shelly').strip(),
        dashboard_url=(env.get('DASHBOARD_URL') or '').strip() or None,
        timezone=timezone,
        profile_path=(env.get('SHELLY_PROFILE') or 'profile.md').strip(),
    )
