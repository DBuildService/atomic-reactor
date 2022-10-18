"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from textwrap import dedent
import json
import logging
import requests
import time
from typing import List, Dict

from atomic_reactor.constants import REMOTE_SOURCE_TARBALL_FILENAME
from atomic_reactor.download import download_url
from atomic_reactor.util import get_retrying_requests_session


logger = logging.getLogger(__name__)

CFG_TYPE_B64 = 'base64'


class CachitoAPIError(Exception):
    """Top level exception for errors in interacting with Cachito's API"""


class CachitoAPIInvalidRequest(CachitoAPIError):
    """Invalid request made to Cachito's API"""


class CachitoAPIUnsuccessfulRequest(CachitoAPIError):
    """Cachito's API request not completed successfully"""


class CachitoAPIRequestTimeout(CachitoAPIError):
    """A request to Cachito's API took too long to complete"""


class CachitoAPI(object):

    def __init__(self, api_url, insecure=False, cert=None, timeout=None):
        self.api_url = api_url
        self.session = self._make_session(insecure=insecure, cert=cert)
        self.timeout = 3600 if timeout is None else timeout

    def _make_session(self, insecure, cert):
        # method_whitelist=False allows retrying non-idempotent methods like POST
        session = get_retrying_requests_session(method_whitelist=False)
        session.verify = not insecure
        if cert:
            session.cert = cert
        return session

    def request_sources(self, repo, ref, flags=None, pkg_managers=None, user=None,
                        dependency_replacements=None, packages=None):
        """Start a new Cachito request

        :param repo: str, the URL to the SCM repository
        :param ref: str, the SCM reference to fetch
        :param flags: list<str>, list of flag names
        :param pkg_managers: list<str>, list of package managers to be used for resolving
                             dependencies
        :param user: str, user the request is created on behalf of. This is reserved for privileged
                     users that can act as cachito representatives
        :param dependency_replacements: list<dict>, dependencies to be replaced by cachito
        :param packages: dict, the packages configuration that allows to specify things such
                         as subdirectories where packages reside in the source repository

        :return: dict, representation of the created Cachito request
        :raise CachitoAPIInvalidRequest: if Cachito determines the request is invalid
        """
        payload = {
            'repo': repo,
            'ref': ref,
            'flags': flags,
            'pkg_managers': pkg_managers,
            'user': user,
            'dependency_replacements': dependency_replacements,
            'packages': packages,
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}

        url = '{}/api/v1/requests'.format(self.api_url)
        logger.debug('Making request %s with payload:\n%s', url, json.dumps(payload, indent=4))
        response = self.session.post(url, json=payload)

        try:
            response_json = response.json()
            logger.debug('Cachito response:\n%s', json.dumps(response_json, indent=4))
        except ValueError:  # json.JSONDecodeError in py3 (is a subclass of ValueError)
            response_json = None

        if response.status_code == requests.codes.bad_request:
            raise CachitoAPIInvalidRequest(response_json['error'])
        response.raise_for_status()
        return response_json

    def wait_for_request(
            self, request, burst_retry=3, burst_length=30, slow_retry=10):
        """Wait for a Cachito request to complete

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key
        :param burst_retry: int, seconds to wait between retries prior to exceeding
                            the burst length
        :param burst_length: int, seconds to switch to slower retry period
        :param slow_retry: int, seconds to wait between retries after exceeding
                           the burst length

        :return: dict, latest representation of the Cachito request
        :raise CachitoAPIUnsuccessfulRequest: if the request completes unsuccessfully
        :raise CachitoAPIRequestTimeout: if the request does not complete timely
        """
        request_id = self._get_request_id(request)
        url = '{}/api/v1/requests/{}'.format(self.api_url, request_id)
        log_url = f'{url}/logs'
        logger.info('Waiting for request %s to complete...', request_id)

        last_updated_value = None
        last_update_time = None
        while True:
            response = self.session.get(url)
            response.raise_for_status()
            response_json = response.json()

            state = response_json['state']
            if state in ('stale', 'failed'):
                state_reason = response_json.get('state_reason') or 'Unknown'
                logger.error(dedent("""\
                   Request %s is in "%s" state: %s
                   Details: %s
                   """), request_id, state, state_reason, json.dumps(response_json, indent=4))
                raise CachitoAPIUnsuccessfulRequest(
                    "Cachito request is in \"{}\" state, reason: {}. "
                    "Request {} ({}) tried to get repo '{}' at reference '{}'.".format(
                        state, state_reason, request_id, log_url,
                        response_json['repo'], response_json['ref']
                    )
                )
            if state == 'complete':
                logger.debug(dedent("""\
                    Request %s is complete
                    Request url: %s
                    """), request_id, url)
                return response_json

            # All other states are expected to be transient and are not checked.

            # If "last_updated_value" does not match the "updated" value of the
            # request from Cachito, then we know Cachito has performed some work
            # since the last check, so the timer resets.
            if last_updated_value is None or last_updated_value != response_json['updated']:
                last_updated_value = response_json['updated']
                last_update_time = time.time()

            elapsed = time.time() - last_update_time
            if elapsed > self.timeout:
                logger.error(dedent("""\
                    Request %s not completed after %s seconds of not being updated
                    Details: %s
                    """), url, self.timeout, json.dumps(response_json, indent=4))
                raise CachitoAPIRequestTimeout(
                    'Request %s not completed after %s seconds of not being updated'
                    % (url, self.timeout))
            else:
                if elapsed > burst_length:
                    time.sleep(slow_retry)
                else:
                    time.sleep(burst_retry)

    def download_sources(self, request, dest_dir='.', dest_filename=REMOTE_SOURCE_TARBALL_FILENAME):
        """Download the sources from a Cachito request

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key
        :param dest_dir: str, existing directory to create file in
        :param dest_filename: str, optional filename for downloaded file
        """
        request_id = self._get_request_id(request)
        logger.debug('Downloading sources bundle from request %ds', request_id)
        url = self.assemble_download_url(request_id)
        dest_path = download_url(
            url, dest_dir=dest_dir, insecure=not self.session.verify, session=self.session,
            dest_filename=dest_filename)
        logger.debug('Sources bundle for request %d downloaded to %s', request_id, dest_path)
        return dest_path

    def assemble_download_url(self, request):
        """Return the URL to be used for downloading the sources from a Cachito request

        :param request: int or dict, either the Cachito request ID or a dict with 'id' key

        :return: str, the URL to download the sources
        """
        request_id = self._get_request_id(request)
        return '{}/api/v1/requests/{}/download'.format(self.api_url, request_id)

    def _get_request_id(self, request):
        if isinstance(request, int):
            return request
        elif isinstance(request, dict):
            return request['id']
        raise ValueError('Unexpected request type: {}'.format(request))

    def get_request_env_vars(self, rid: int) -> Dict[str, Dict[str, str]]:
        """Get the environment variables from endpoint /requests/$id/environment-variables

        :param int rid: the Cachito request id whose environment variables will
            be returned.
        :return: a mapping containing the environment variables. For example:
            ``{"GOCACHE": {"kind": "path", "value": "deps/gomod"}, ...}``.
        :rtype: dict[str, dict[str, str]]
        :raises: ValueError if the server does not response a JSON data to be
            parsed.
        :raises: HTTP error raised from the underlying requests library in any
            case of the request cannot be completed.
        """
        endpoint = f'{self.api_url}/api/v1/requests/{rid}/environment-variables'
        resp = self.session.get(endpoint)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as exc:
            msg = (f'JSON data is expected from Cachito endpoint {endpoint}, '
                   f'but the response contains: {resp.content}.')
            raise ValueError(msg) from exc

    def get_request_config_files(self, rid: int) -> List[Dict[str, str]]:
        """Get the configuration files from endpoint /requests/$id/configuration-files

        :param int rid: the Cachito request id whose configuration files will be returned.
        :return: a list of configuration files. For example:
            ``[{"path": "app/.npmrc", "type": "base64", "content": "<base64 encoded content>"}]``.
        :rtype: list[dict[str, str]]
        :raises: ValueError if the server does not response a JSON data to be
            parsed.
        :raises: HTTP error raised from the underlying requests library in any
            case of the request cannot be completed.
        """
        endpoint = f'{self.api_url}/api/v1/requests/{rid}/configuration-files'
        resp = self.session.get(endpoint)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as exc:
            msg = (f'JSON data is expected from Cachito endpoint {endpoint}, '
                   f'but the response contains: {resp.content}.')
            raise ValueError(msg) from exc

    def get_image_content_manifest(self, request_ids: List[int]) -> dict:
        """
        Get the image content manifest from endpoint /content-manifest
        :param list[int] request_ids: cachito request ids
        :return: image content manifest data
        :rtype: dict
        :raises: ValueError if the server does not response a JSON data to be
            parsed.
        :raises: HTTP error raised from the underlying requests library in any
            case of the request cannot be completed.
        """
        endpoint = f'{self.api_url}/api/v1/content-manifest'
        resp = self.session.get(endpoint, params={'requests': ','.join(map(str, request_ids))})
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as exc:
            msg = (f'JSON data is expected from Cachito endpoint {endpoint}, '
                   f'but the response contains: {resp.content}.')
            raise ValueError(msg) from exc


if __name__ == '__main__':
    logging.basicConfig()
    logger.setLevel(logging.DEBUG)

    # See instructions on how to start a local instance of Cachito:
    #   https://github.com/release-engineering/cachito
    api = CachitoAPI('http://localhost:8080', insecure=True)
    response = api.request_sources(
        'https://github.com/release-engineering/retrodep.git',
        'e1be527f39ec31323f0454f7d1422c6260b00580',
    )
    request_id = response['id']
    api.wait_for_request(request_id)
    api.download_sources(request_id)
