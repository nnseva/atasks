"""
# Typical usage scenarios for atasks with a single namespace

Run this script using the command below to execute the scenario tests.

## Simplest loopback run. Uses the default namespace in loopback mode.

Requests itself for run some tasks calling the defined tasks in loopback mode.

```bash
python -m atasks.run -L atasks dev -v 4 dev.tests.scenarios
```

## AMQP loopback run. Uses the default namespace with the AMQP transport in loopback mode.

Requests itself for run some tasks calling the defined tasks in loopback mode.
```bash
python -m atasks.run -L atasks dev -v 4 -N transport=amqp dev.tests.scenarios
```

## AMQP client/server run. Uses the default namespace with the AMQP transport.

The server instance doesn't request any tasks itself, it only serves incoming requests:

```bash
python -m atasks.run -L atasks dev -v 4 -N transport=amqp,mode=server dev.tests.scenarios
```

Run server instances as many as you want.

The client instance requests server instance(s) (started as above) for run some
tasks calling the defined tasks.

```bash
python -m atasks.run -L atasks dev -v 4 -N transport=amqp,mode=client dev.tests.scenarios
```

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
    if default_ns is None:
        logger.error("No default namespace found, the scenario cannot proceed.")
        return

    if default_ns['mode'] in ('client', 'loopback'):
        logger.info("Default namespace is in client/loopback mode, initiating requests.")
        a = await task_one(42)
        assert a == 42

        a = await task_three(24)
        assert a == 24

        await request_sequence()
        returns = await request_parallel()
        assert returns == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
    else:
        logger.info("Default namespace is in server mode, waiting for incoming requests.")
