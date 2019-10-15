"""
Processed scenarios
"""

import asyncio
import logging

from atasks.tasks import atask


logger = logging.getLogger(__name__)


@atask
async def task_one(a):
    """Example task"""
    logger.info("task_one starting: {}".format(a))
    await asyncio.sleep(1)
    logger.info("task_one finished: {}".format(a))
    return a


@atask
async def task_two(a):
    """Another example task"""
    logger.info("task_two starting: {}".format(a))
    await asyncio.sleep(2)
    logger.info("task_two finished: {}".format(a))
    return a


@atask
async def task_three(a):
    """Yet another example task"""
    logger.info("task_three evaluating: {}".format(a))
    return a


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

    if options['mode'] in ('client', 'loopback'):
        a = await task_one(42)
        assert a == 42

        a = await task_three(24)
        assert a == 24

        await request_sequence()
        returns = await request_parallel()
        assert returns == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
