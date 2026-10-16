''' python -m shelly.simplefin_login: give Shelly read access to the operator's bank accounts, once

On bridge.simplefin.org: connect the banks, then make a setup token for a new
app and paste it here. The token is exchanged (once -- it is useless after) for
an access URL, saved to SIMPLEFIN_TOKEN_FILE (default secrets/simplefin.json),
readable only by this user. Restart the service afterwards.
'''

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from shelly.integrations.simplefin import SimpleFin, SimpleFinError, claim, errors, save


async def login(setup_token, path):
    async with httpx.AsyncClient(timeout=60) as http:
        access = await claim(setup_token, http)
        # Saved before anything else can fail: the token cannot be claimed twice.
        save(path, access)
        account_set = await SimpleFin(access, http).accounts(balances_only=True)

    return account_set


def main():
    load_dotenv()
    path = Path(os.environ.get('SIMPLEFIN_TOKEN_FILE') or 'secrets/simplefin.json')

    if path.exists():
        print(f'{path} already exists. A new token replaces it; Ctrl-C to keep the old one.\n')

    print('On bridge.simplefin.org: connect your banks, then create a setup token for a new app')
    print('("Shelly") and paste it here.\n')
    token = input('Setup token: ').strip()

    try:
        account_set = asyncio.run(login(token, path))
    except SimpleFinError as error:
        print(f'\nThat did not work: {error}')
        return 1

    accounts = account_set.get('accounts') or []
    print(f'\nDone. Saved to {path}. SimpleFIN shows {len(accounts)} account(s):')

    for account in accounts:
        print(f'  - {account.get("name")}')

    for message in errors(account_set):
        print(f'  ! {message}')

    print('\nRestart Shelly to use it.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
