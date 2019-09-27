# ATasks

ATasks executes async tasks in separate processes distributed among a network.

The idea of ATasks has been inspired by asyncio, [Celery](https://docs.celeryproject.org/en/latest)
and [aiotasks](https://github.com/cr0hn/aiotasks) packages.

The main advantages of the ATasks comparing with Celery:
- asynchronous task evaluation instead of synchronous tasks
- free combining of task calls inside another task using await
- easy awaiting a task and getting a result
- parallelization using standard asynchronous syntax
- no any restriction for recurrent awaits

The main advantages of the ATasks comparing with aiotasks:
- easier getting a result (await instead of async with)
- full transparency - the only difference from usual
  coroutine await is distributing tasks evaluation
  among a network
- actual development

The main disadvantages with Celery and aiotasks:
- delay(), send(), async_call(), s() etc. syntax is not available,
  and will never be implemented

Usual scenarios see in the [scenarios.py](dev/tests/scenarios.py) file.

## Where the task is evaluated

After the task is started, it is running in one thread from the beginning
to the end. Other tasks may share the same thread in an asynchronous manner.

On the other side, another task called from the first one may be
running either in another process on another host or in the same
thread, depending on the decision taken on the transport layer.

The point where the task is awaited is the only point of taking
a decision, where the awaited task should be running.

If the task is awaited in the AIOSteve worker process, it may be running
in the same, or another process. As opposite, when the task is awaited in
the standalone program, it may be running only in the AIOSteve worker process,
except a case when you explicitly set up the transport layer to the special
Loopback transport. All tasks are running in the same thread in this case.
Such transport may be used for testing purposes.

## How to track a task

???

## When the task is crashed

???

## When the worker evaluating a task is crashed

???

## How to await a task in synchronous program

???
