''' Google's APIs for the operator's own account: the saved login, and the Calendar calls

Plain REST over httpx rather than Google's client library: that library is
synchronous and large, and Shelly needs only a handful of calls. The login is
done once, by hand, with `python -m shelly.google_login`, which saves a refresh
token; from it an access token is fetched here and renewed shortly before it
expires. Nothing from the login is ever stored in Mongo or logged.
'''

import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

import httpx


log = logging.getLogger(__name__)

TOKEN_URL = 'https://oauth2.googleapis.com/token'
CALENDAR = 'https://www.googleapis.com/calendar/v3'

# What the login asks for. calendar.events: read and change events;
# calendar.readonly: list the calendars themselves. Adding a scope here
# means logging in again.
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.readonly',
]

RELOGIN = ('Google access has expired or was revoked. The operator needs to log in again on '
           'the Jetson: python -m shelly.google_login')


class GoogleError(Exception):
    ''' a Google call that did not work; the message is written for Claude '''


class GoogleAuth:
    ''' access tokens from the saved refresh token, renewed a minute before they expire '''

    def __init__(self, token_path, http):
        saved = json.loads(Path(token_path).read_text())
        self._client_id = saved['client_id']
        self._client_secret = saved['client_secret']
        self._refresh_token = saved['refresh_token']
        self.scopes = saved.get('scopes', [])
        self.http = http
        self._token = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self):
        async with self._lock:
            if self._token is None or time.monotonic() > self._expires - 60:
                await self._refresh()

            return self._token

    def forget(self):
        self._token = None

    async def _refresh(self):
        response = await self.http.post(TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'client_id': self._client_id,
            'client_secret': self._client_secret,
            'refresh_token': self._refresh_token,
        })

        if response.status_code != 200:
            # invalid_grant: revoked, or the password changed, or the app was
            # left in Testing mode and its week ran out.
            log.error('google token refresh failed: %s %s', response.status_code, response.text[:200])
            raise GoogleError(RELOGIN)

        body = response.json()
        self._token = body['access_token']
        self._expires = time.monotonic() + body.get('expires_in', 3600)

    async def request(self, method, url, **kwargs):
        ''' one authorised call; returns the JSON body (None if empty), raises GoogleError

        A 401 is retried once with a fresh token: the token can be revoked
        before it says it expires.
        '''

        for attempt in (1, 2):
            headers = {'Authorization': f'Bearer {await self.token()}'}

            try:
                response = await self.http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as error:
                raise GoogleError(f'Could not reach Google: {type(error).__name__}') from error

            if response.status_code == 401 and attempt == 1:
                self.forget()
                continue

            break

        if response.status_code >= 400:
            try:
                message = response.json()['error']['message']
            except (ValueError, KeyError, TypeError):
                message = response.text[:200]

            raise GoogleError(f'Google said {response.status_code}: {message}')

        return response.json() if response.content else None


class GoogleCalendar:
    ''' the Calendar v3 calls Shelly uses '''

    def __init__(self, auth):
        self.auth = auth

    @staticmethod
    def _events(calendar_id):
        # Calendar ids are email-like and holiday ones contain '#'.
        return f'{CALENDAR}/calendars/{quote(calendar_id, safe="")}/events'

    async def calendars(self):
        ''' every calendar on the operator's list '''

        body = await self.auth.request('GET', f'{CALENDAR}/users/me/calendarList',
                                       params={'minAccessRole': 'reader', 'maxResults': 250})

        return body.get('items', [])

    async def events(self, calendar_id, start, end, query=None, limit=250):
        ''' events overlapping [start, end), recurring ones expanded, in start order '''

        params = {
            'timeMin': start.isoformat(),
            'timeMax': end.isoformat(),
            'singleEvents': 'true',
            'orderBy': 'startTime',
            'maxResults': limit,
        }

        if query:
            params['q'] = query

        body = await self.auth.request('GET', self._events(calendar_id), params=params)

        return body.get('items', [])

    async def event(self, calendar_id, event_id):
        return await self.auth.request('GET', f'{self._events(calendar_id)}/{quote(event_id, safe="")}')

    async def insert(self, calendar_id, body, notify):
        return await self.auth.request('POST', self._events(calendar_id), json=body,
                                       params={'sendUpdates': 'all' if notify else 'none'})

    async def patch(self, calendar_id, event_id, body, notify):
        return await self.auth.request('PATCH', f'{self._events(calendar_id)}/{quote(event_id, safe="")}',
                                       json=body, params={'sendUpdates': 'all' if notify else 'none'})

    async def delete(self, calendar_id, event_id, notify):
        await self.auth.request('DELETE', f'{self._events(calendar_id)}/{quote(event_id, safe="")}',
                                params={'sendUpdates': 'all' if notify else 'none'})
