''' SimpleFIN Bridge: read-only access to the operator's bank and card accounts

The operator connects their banks on bridge.simplefin.org and gets a one-time
setup token: base64 of a claim URL. Claiming it (POST, once) returns an access
URL with Basic Auth credentials in it, which `python -m shelly.simplefin_login`
saves to SIMPLEFIN_TOKEN_FILE. That URL is full read access to every connected
account: it is never stored in Mongo or logged.

The Bridge expects about 24 requests a day and refreshes data about once a day,
so Shelly syncs on a schedule into Mongo (capabilities/finance.py) rather than
calling here per question. One request covers at most 90 days.
'''

import base64
import binascii
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


MAX_DAYS = 90

COMPROMISED = ('That setup token does not exist or was already claimed. If you did not claim '
               'it yourself, someone else may have: disable it on bridge.simplefin.org and make '
               'a new one.')


class SimpleFinError(Exception):
    ''' a SimpleFIN call that did not work; the message is written for the operator and Claude

    status is the HTTP status when the Bridge answered with one: 402 means the
    subscription lapsed, 403 that the access was revoked.
    '''

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def claim_url(setup_token):
    ''' the claim URL inside a setup token, or SimpleFinError '''

    try:
        url = base64.b64decode(''.join(setup_token.split()), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise SimpleFinError('That is not a SimpleFIN setup token (it should be one long base64 string).')

    if urlsplit(url).scheme != 'https':
        raise SimpleFinError('That setup token does not hold an https address; refusing to use it.')

    return url


async def claim(setup_token, http):
    ''' exchange a setup token for the access URL; works once per token '''

    try:
        response = await http.post(claim_url(setup_token))
    except httpx.HTTPError as error:
        raise SimpleFinError(f'Could not reach SimpleFIN: {type(error).__name__}') from error

    if response.status_code == 403:
        raise SimpleFinError(COMPROMISED, 403)

    if response.status_code != 200:
        raise SimpleFinError(f'SimpleFIN said {response.status_code} to the claim.', response.status_code)

    access = response.text.strip()
    parts = urlsplit(access)

    if parts.scheme != 'https' or not parts.username:
        raise SimpleFinError('SimpleFIN returned something that is not an access URL.')

    return access


def save(path, access_url):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    with os.fdopen(descriptor, 'w') as out:
        json.dump({'access_url': access_url}, out)

    os.chmod(path, 0o600)


class SimpleFin:
    ''' GET /accounts with the saved access URL's credentials '''

    def __init__(self, access_url, http):
        parts = urlsplit(access_url)
        self._auth = (parts.username or '', parts.password or '')
        # The credentials go in the Authorization header, never in a URL that could be logged.
        host = parts.hostname + (f':{parts.port}' if parts.port else '')
        self._base = urlunsplit((parts.scheme, host, parts.path.rstrip('/'), '', ''))
        self.http = http

    @classmethod
    def load(cls, path, http):
        return cls(json.loads(Path(path).read_text())['access_url'], http)

    async def accounts(self, start=None, end=None, balances_only=False):
        ''' the account set: every account, with transactions posted in [start, end)

        start and end are aware datetimes. Pending transactions are included
        when the bank reports them.
        '''

        params = {'pending': 1}

        if start is not None:
            params['start-date'] = int(start.timestamp())

        if end is not None:
            params['end-date'] = int(end.timestamp())

        if balances_only:
            params['balances-only'] = 1

        try:
            response = await self.http.get(f'{self._base}/accounts', params=params, auth=self._auth)
        except httpx.HTTPError as error:
            raise SimpleFinError(f'Could not reach SimpleFIN: {type(error).__name__}') from error

        if response.status_code == 402:
            raise SimpleFinError('SimpleFIN says payment is required: the Bridge subscription has '
                                 'lapsed. Renew it on bridge.simplefin.org.', 402)

        if response.status_code == 403:
            raise SimpleFinError('SimpleFIN refused the saved access (revoked or disabled). Make a new '
                                 'setup token on bridge.simplefin.org and run '
                                 'python -m shelly.simplefin_login.', 403)

        if response.status_code != 200:
            raise SimpleFinError(f'SimpleFIN said {response.status_code}.', response.status_code)

        try:
            return response.json()
        except ValueError:
            raise SimpleFinError('SimpleFIN returned something that is not JSON.')


def errors(account_set):
    ''' the error messages in an account set, from either protocol version '''

    found = [e.get('msg', '') for e in account_set.get('errlist') or [] if isinstance(e, dict)]
    found += [e for e in account_set.get('errors') or [] if isinstance(e, str)]

    return [e for e in found if e]


def institutions(account_set):
    ''' connection id -> institution name (version 2), for accounts that name only a conn_id '''

    return {c.get('conn_id'): c.get('name') or c.get('org_name')
            for c in account_set.get('connections') or [] if isinstance(c, dict)}
