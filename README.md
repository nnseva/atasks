# ATasks

## Installation

*Stable version* from the PyPi package repository

```bash
pip install atasks
```

*Last development version* from the GitHub source version control system
```
pip install git+git://github.com/nnseva/atasks.git
```

## Initializiation

Before execution some number of core objects should be initialized.

```python
    from atasks.transport.backends.amqp import AMQPTransport
    from atasks.router import get_router
    from atasks.codecs import PickleCodec

    PickleCodec()
    transport = AMQPTransport()
    await transport.connect()

    if mode == 'server':
        router = get_router()
        await router.activate(transport)
```

### Codec

The codec determines a way to encode and decode objects passed through the network.
It should support as many types as it can.

The `atasks.codecs.PickleCodec` provided by the package uses standard python `pickle` package.
It is universal but not always safe solution.

User can initialize the own codec implementation using `atasks.codecs.Codec` as a base
class. Just replace all methods generating `NotImplementedError`. Note that
most of methods are asynchronous.

```python
from atasks.codecs import PickleCodec

...
    PickleCodec()
```

Note that the codec is installed into the system while construction.

### Transport

Transport determines the method of sending requests and returning results
from awaiter to the performing coroutine.

The `atasks.transport.base.LoopbackTransport` provided by the package passes
all requests back to the awaiter thread only. You can use it for the testing purposes.

The `atasks.transport.backends.amqp.AMQPTransport` provided by the package passes
requests through the RabbitMQ or other AMQP broker to any ATasks worker started
on the same or another host.

Any transport should be connected after creation. The `connect()` method is asynchronous.

```python
    from atasks.transport/base import LoopbackTransport
    from atasks.transport.backends.amqp import AMQPTransport

    ...
    if transport == 'loopback':
        LoopbackTransport()
    elif transport == 'amqp':
        AMQPTransport()
    await transport.connect()
```

Other transport kinds may be implemented later.

User can instantiate the own transport implementation using `atasks.transport.base.Transport` as a base
class. Just replace all methods generating `NotImplementedError`. Note that
most of methods are asynchronous.


### Router

Router determines a way how the reference looks like, how it is awaited,
what data are passed over the network etc. Router is a core of the ATasks package.

The `atasks.router.Router` is an only default router implementation.

User can inherit and instantiate the own router implementation if necessary.

As a rule, you don't need to do it. In this case, you can just
use `get_router()` function to get a default router instance.

```python
from atasks.router import get_router

...
    router = get_router()
```

#### Client and Server

If your application should send requests only, no any
other actions required on the initialization stage.

Server application which listens to events should
activate a transport to send requests.

```python
    server = AMQPTransport()

    ...
    router = get_router()
    await router.activate(server)
```

## Markup an asynchronous distributed task

Decorator `atasks.tasks.atask` is used to markup the asynchronous coroutine
(or even synchronous returning `future` object) as an asynchronous distributed
task.

Note that the first call to the wrapper creates a default router. You should
create your own Router (or ancestor) instance before the first call
to the wrapper if necessary.

```python
@atask
async def some_task(a):
    ...
```

## Awaiting evaluation of the asynchronous distributed task

Just await a decorated asynchronous remote procedure exactly same as any local coroutine.

```python
@atask
async def some_task(a):
    ret = await some_other_task(a)

@atask
async def some_other_task(a):
    ...

async def not_a_task_just_coro():
    a = await some_task(42)
    ...

```

## Namespaces

Objects may be instantiated in separate namespaces. Just
pass an additional `namespace=...` parameter to:

- constructor of codec, transport, or route object
- atask decorator
- `get_route`, `get_transport`, or `get_codec` function

One namespace is completely separated from anoher. Every
namespace uses it's own set of router, transport, and codec.

The default namespace has a name `default`.

You can await task from one namespace in another.

```python
@atask(namespace='one')
async def some_task():
    await some_other_task()
    ...

@atask(namespace='other')
async def some_other_task():
    ....
```

## Inspiration

The idea of ATasks has been inspired by `asyncio`, [Celery](https://docs.celeryproject.org/en/latest)
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
- `delay()`, `send()`, `async_call()`, `s()` etc. syntax is not available,
  and will never be implemented

Usual scenarios see in the [scenarios.py](dev/tests/scenarios.py) file.


## Where the task is evaluated

After the task is started, it is running in one thread from the beginning
to the end. Other tasks may share the same thread in an asynchronous manner.

On the other side, another task called from the first one may be
running on any ATasks worker, on the same as the first one, or another,
depending on the decision taken on the transport layer.

The point where the task is awaited is the only point of taking
a decision, where the awaited task should be running. The transport layer
takes  this decision.

The ATasks application can issue remote awaits immediately after
core objects initializing. The ATask application receives remote
awaits after the `activate()` call of the `Router`.

The `LoopbackTransport` always passes all awaits immediately to
coroutines in the same thread. It may be used for testing purposes.

Other `Transport`s may allow remote awaits inside a process,
or a host, or passed among a network.

The `AMQPTransport` allows using RabbitMQ (or analogue) to
pass remote awaits among a network to any number
of instances.

## How to track a task

???

## When the task is crashed

???

## When the worker evaluating a task is crashed

???

## How to await a task in synchronous program

???
