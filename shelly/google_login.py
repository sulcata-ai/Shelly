''' python -m shelly.google_login: give Shelly access to the operator's Google account, once

Prints a Google link to open in any browser -- the phone, a laptop, the Jetson's
own. After approving, the browser lands on a localhost address that will not
load; that address carries the one-time code, so it is pasted back here. That
way it works the same whether or not the browser is on the Jetson.

Saves the refresh token to GOOGLE_TOKEN_FILE (default secrets/google_token.json),
readable only by this user. Restart the service afterwards.
'''

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow

from shelly.integrations.google import SCOPES


REDIRECT = 'http://localhost:8765/'


def main():
    load_dotenv()
    client = Path(os.environ.get('GOOGLE_CLIENT_FILE') or 'secrets/google_client.json')
    token = Path(os.environ.get('GOOGLE_TOKEN_FILE') or 'secrets/google_token.json')

    if not client.exists():
        print(f'No OAuth client at {client}. Download it from Google Cloud (Clients, Desktop app).')
        return 2

    flow = Flow.from_client_secrets_file(str(client), SCOPES, redirect_uri=REDIRECT)
    # prompt=consent: Google only hands out a refresh token when consent is given.
    url, _ = flow.authorization_url(access_type='offline', prompt='consent')

    print('1. Open this link in any browser and sign in with your Google account:\n')
    print(url)
    print('\n2. If Google warns the app is unverified: Advanced, then Go to Shelly. Allow access.')
    print('3. The browser ends on a localhost page that does not load. That is expected.')
    print('   Copy the whole address from the address bar and paste it here.\n')

    answer = input('Address: ').strip()

    # The redirect is plain http to localhost, which oauthlib refuses by default;
    # nothing travels over it here -- the address is pasted, not served.
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

    try:
        flow.fetch_token(authorization_response=answer)
    except Exception as error:
        print(f'\nThat did not work: {error}\nRun this again and paste the full address.')
        return 1

    credentials = flow.credentials

    if not credentials.refresh_token:
        print('\nGoogle returned no refresh token. Remove Shelly at '
              'https://myaccount.google.com/connections and run this again.')
        return 1

    missing = set(SCOPES) - set(credentials.granted_scopes or SCOPES)

    if missing:
        print(f'\nSome access was not granted: {", ".join(sorted(missing))}. Run this again and '
              'tick every box.')
        return 1

    token.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    saved = {
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'refresh_token': credentials.refresh_token,
        'scopes': SCOPES,
    }
    descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    with os.fdopen(descriptor, 'w') as out:
        json.dump(saved, out)

    os.chmod(token, 0o600)
    print(f'\nDone. Saved to {token}. Restart Shelly to use it.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
