"""
Processed scenarios
"""

import asyncio
import logging

from atasks import trace
from atasks.tasks import atask


logger = logging.getLogger(__name__)


@atask
async def task_one(a):
    """Example task"""
    logger.info("task_one starting: {}".format(a))
    await asyncio.sleep(0.1)
    logger.info("task_one finished: {}".format(a))
    return a


@atask
async def task_two(a):
    """Another example task"""
    logger.info("task_two starting: {}".format(a))
    await asyncio.sleep(0.2)
    logger.info("task_two finished: {}".format(a))
    return a


@atask
async def task_three(a):
    """Yet another example task"""
    logger.info("task_three evaluating: {}".format(a))
    return a


@atask
async def task_except_top(a):
    """Exception from sub-atask"""
    try:
        await task_except(a)
    except Exception as e:
        logger.error("task_except_top encountered an exception: {}".format(e))
        print(trace.format_trace(e))
        raise


@atask
async def task_except(a):
    """Exception example task"""
    logger.info("task_except starting: {}".format(a))
    try:
        await async_except(a)
    except Exception as e:
        logger.error("task_except encountered an exception: {}".format(e))
        print(trace.format_trace(e))
        raise


async def async_except(a):
    """Internal exception example"""
    logger.info("async_except starting: {}".format(a))
    try:
        raise Exception("Intentional exception in async_except({})".format(a))
    except Exception as e:
        logger.error("async_except encountered an exception: {}".format(e))
        print(trace.format_trace(e))
        raise


@atask
async def request_sequence():
    """Example task calling and processing sequence of another tasks"""
    logger.info("request_sequence starting")
    a = await task_one(1)
    assert a == 1
    a = await task_two(3)
    assert a == 3
    logger.info("request_sequence finished")


@atask
async def request_parallel():
    """Example task calling and processing bunch of another tasks"""
    logger.info("request_parallel starting")
    futures = [task_one(a) for a in range(5)] + [task_two(a) for a in range(5)]
    returns = await asyncio.gather(*futures)
    assert returns == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
    logger.info("request_parallel finished")
    return returns


async def aiomain(**options):
    """The non-task main function calls tasks from atasks worker, not self process"""
    default_ns = {ns['name']: ns for ns in options['namespaces']}.get('default')

    # A 'client' namespace expects some other, already-running process to
    # serve 'default' (e.g. over amqp) - it's safe (and the point) to call
    # into it here. A 'server' namespace only self-calls when paired with the
    # loopback transport: that transport only ever reaches a Router activated
    # in this very process (see README.md#commands), so this is the one-process
    # server-and-client combination formerly known as run.py's 'loopback' mode -
    # for a dedicated amqp-backed server, self-calling here would be wrong.
    is_remote_client = default_ns and default_ns['mode'] == 'client'
    is_self_hosted_server = default_ns and default_ns['mode'] == 'server' and default_ns['transport'] == 'loopback'
    if is_remote_client or is_self_hosted_server:
        a = await task_one(42)
        assert a == 42

        a = await task_three(24)
        assert a == 24

        await request_sequence()
        returns = await request_parallel()
        assert returns == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
