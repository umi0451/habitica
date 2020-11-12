#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import json, re
import time
import logging
import pkg_resources
from pathlib import Path
logging.captureWarnings(True)
import requests
import requests.adapters
import urllib3
import urllib3.util.retry
logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)

USER_ID_FILE = Path(pkg_resources.resource_filename('habitica', 'data/USER_ID'))
if not USER_ID_FILE.exists(): # TODO duplicates code from setup.py. Needs to be moved to habitica.config and re-used.
    print('File {0} is missing.'.format(USER_ID_FILE))
    print('File {0} should be present in the root directory and should contain Habitica User ID of the author of the package.'.format(USER_ID_FILE))
    print('For forked project it is advisable to use your own User ID (see https://habitica.com/user/settings/api)')
    sys.exit(1)
USER_ID = USER_ID_FILE.read_text().strip()
if not re.match(r'^[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}$', USER_ID, flags=re.I):
    print('File {0} contains invalid user_id: {1}'.format(USER_ID_FILE, repr(USER_ID)))
    print('Please ensure that proper User ID is used (see https://habitica.com/user/settings/api)')
    sys.exit(1)

class API(object):
    """ Basic API facade. """
    TIMEOUT = 10.0 # Call timeout.
    MAX_RETRY = 3 # Amount of retries for server errors (5xx, timeouts etc).

    # Third-party API tools should introduce delays between calls
    # to reduce load on Habitica server.
    # See https://habitica.fandom.com/wiki/Template:Third_Party_Tool_Rules?section=T-4
    DEFAULT_REQUEST_DELAY = 0.5 # sec
    GET_AUTO_REQUEST_DELAY = 3 # sec
    POST_AUTO_REQUEST_DELAY = 10 # sec

    def __init__(self, base_url, login, password, batch_mode=True):
        """ Creates authenticated API instance.
        If batch_mode is True (default), introduces significant delays
        between consequent requests to reduce load on Habitica server.
        Otherwise (for user input) uses default nominal delay <1 sec.
        """
        self.batch_mode = batch_mode
        self.base_url = base_url
        self.login = login
        self.password = password
        self.headers = {
              'x-api-user': login,
              'x-api-key': password,
              'x-client': USER_ID + '-habitica', # TODO take appName from package?
              'content-type': 'application/json',
              }
        self._last_request_time = 0
    def get_url(self, *parts):
        """ Makes URL to call specified .../subpath/of/parts. """
        return '/'.join([self.base_url, 'api', 'v3'] + list(parts))
    def call(self, method, uri, data):
        """ Performs actual call to URI using given method (POST/GET etc).
        Data should correspond to specified method.
        For POST/PUT methods, if field '_params' is present,
        it is extracted and passed as request params.
        May raise exceptions from requests.
        May freeze for several seconds to ensure delay between requests
        (see POST_REQUEST_DELAY, GET_REQUEST_DELAY)
        """
        if self.batch_mode:
            delay = self.DEFAULT_REQUEST_DELAY
        elif method.upper() == 'POST':
            delay = self.POST_REQUEST_DELAY
        else:
            delay = self.GET_REQUEST_DELAY
        delay = delay - (time.time() - self._last_request_time)
        if delay > 0:
            time.sleep(delay)
        return self._retry_call(method, uri, data)
    def _retry_call(self, method, uri, data, tries=MAX_RETRY):
        try:
            return self._direct_call(method, uri, data)
        except requests.exceptions.ReadTimeout as e:
            if tries <= 0:
                raise
        except requests.exceptions.HTTPError as e:
            if e.response.status_code not in [502]:
                raise
            if tries <= 0:
                raise
        except requests.exceptions.ConnectionError as e:
            if tries <= 0:
                raise
        return self._retry_call(method, uri, data, tries=tries-1)
    def _direct_call(self, method, uri, data):
        """ Direct call without any retry/timeout checks. """
        session = requests.Session()
        retries = urllib3.util.retry.Retry(total=5, backoff_factor=0.1)
        session.mount('https://', requests.adapters.HTTPAdapter(max_retries=retries))

        if method in ['put', 'post']:
            params = data.get('_params', None)
            if '_params' in data:
                del data['_params']
            return getattr(session, method)(uri, headers=self.headers,
                    params=params, data=json.dumps(data), timeout=API.TIMEOUT)
        else:
            return getattr(session, method)(uri, headers=self.headers,
                                            params=data, timeout=API.TIMEOUT)

class Habitica(object):
    """
    A minimalist Habitica API class.
    """

    def __init__(self, auth=None, resource=None, aspect=None, _api=None):
        self.api = _api or API(auth['url'], auth['x-api-user'], auth['x-api-key'])
        self.resource = resource
        self.aspect = aspect

    def __getattr__(self, m):
        try:
            return object.__getattr__(self, m)
        except AttributeError:
            if not self.resource:
                return Habitica(_api=self.api, resource=m)
            else:
                return Habitica(_api=self.api, resource=self.resource,
                                aspect=m)

    def __getitem__(self, m):
        try:
            return object.__getitem__(self, m)
        except AttributeError:
            if not self.resource:
                return Habitica(_api=self.api, resource=m)
            else:
                res = self.resource + '/' + str(m)
                return Habitica(_api=self.api, resource=res)

    def __call__(self, **kwargs):
        method = kwargs.pop('_method', 'get')

        # build up URL... Habitica's api is the *teeniest* bit annoying
        # so either i need to find a cleaner way here, or i should
        # get involved in the API itself and... help it.
        if self.aspect:
            aspect_id = kwargs.pop('_id', None)
            direction = kwargs.pop('_direction', None)
            if aspect_id is not None:
                uri = self.api.get_url(
                                          self.resource,
                                          self.aspect,
                                          str(aspect_id))
            else:
                uri = self.api.get_url(
                                       self.resource,
                                       self.aspect)
            if direction is not None:
                uri = '%s/%s' % (uri, direction)
        else:
            uri = self.api.get_url(self.resource)

        try:
            res = self.api.call(method, uri, kwargs)
        except:
            logging.exception('Failed to perform API call {0} {1} <= {2}'.format(method.upper(), uri, kwargs))
            return None
        if not res:
            return res

        logging.debug(res.url)
        if res.status_code == requests.codes.ok:
            return res.json()['data']
        else:
            if res.status_code == 404:
                logging.error('URI not found: {0}'.format(uri), file=sys.stderr)
            res.raise_for_status()
