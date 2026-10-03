"""Required RabbitMQ capabilities for AMQP integration tests."""
import asyncio
import base64
import os
import sys
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import aio_pika


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')
MANAGEMENT_URL = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_URL', 'http://localhost:15672')
MANAGEMENT_USER = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_USER', 'guest')
MANAGEMENT_PASSWORD = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_PASSWORD', 'guest')
_SKIP_VALUES = ('1', 'true', 'yes')


def amqp_tests_disabled():
    """Whether the caller explicitly opted out of AMQP integration tests."""
    return os.environ.get('ATASKS_SKIP_AMQP_TESTS', '').lower() in _SKIP_VALUES


async def require_amqp():
    """Verify that the configured AMQP broker accepts connections.

    :returns: ``False`` only for the explicit local opt-out.
    :raises AssertionError: if the configured broker cannot be reached.
    """
    if amqp_tests_disabled():
        return False
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
    except Exception as error:
        raise AssertionError('AMQP broker is unavailable at %s: %s' % (AMQP_URL, error)) from error
    return True


def _management_request(path, method='GET'):
    authorization = base64.b64encode(('%s:%s' % (MANAGEMENT_USER, MANAGEMENT_PASSWORD)).encode()).decode()
    request = urlrequest.Request(
        MANAGEMENT_URL + path,
        headers={'Authorization': 'Basic ' + authorization},
        method=method,
    )
    try:
        with urlrequest.urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except URLError as error:
        raise AssertionError('RabbitMQ management API is unavailable at %s: %s' % (MANAGEMENT_URL, error)) from error


async def require_management_api():
    """Verify the management API, credentials, and connection-delete permission."""
    if amqp_tests_disabled():
        return False
    loop = asyncio.get_running_loop()
    overview_status = await loop.run_in_executor(None, _management_request, '/api/overview')
    connections_status = await loop.run_in_executor(None, _management_request, '/api/connections')
    delete_status = await loop.run_in_executor(
        None,
        _management_request,
        '/api/connections/atasks-preflight-nonexistent',
        'DELETE',
    )
    if overview_status != 200 or connections_status != 200 or delete_status in (401, 403):
        raise AssertionError(
            'RabbitMQ management API at %s is missing required access '
            '(GET /api/overview=%s, GET /api/connections=%s, DELETE /api/connections/*=%s)'
            % (MANAGEMENT_URL, overview_status, connections_status, delete_status),
        )
    return True


async def verify_environment():
    """Verify every capability needed by the AMQP integration suite."""
    if not await require_amqp():
        print('AMQP integration tests are explicitly disabled by ATASKS_SKIP_AMQP_TESTS.')
        return
    await require_management_api()
    print('AMQP integration environment is ready: %s; management API: %s' % (AMQP_URL, MANAGEMENT_URL))


if __name__ == '__main__':
    try:
        asyncio.run(verify_environment())
    except AssertionError as error:
        print('AMQP integration environment check failed: %s' % error, file=sys.stderr)
        raise SystemExit(1)
