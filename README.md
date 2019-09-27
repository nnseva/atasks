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

The transport determines a way to pass requests and return responses through the network.

The `atasks.transport.base.LoopbackTransport` provided by the package passes
all requests only inside a thread back. You can use it for the testing purposes.

The `atasks.transport.backends.amqp.AmqpTransport` provided by the package passes
requests through the RabbitMQ or other AMQP broker.

Other transport kinds may be implemented later.

User can initialize the own transport implementation using `atasks.transport.base.Transport` as a base
class. Just replace all methods generating `NotImplementedError`. Note that
most of methods are asynchronous.

```python
from atasks.transport/base import LoopbackTransport
from atasks.transport.backends.amqp import AmqpTransport

...
    if 'transport' == 'loopback':
        LoopbackTransport()
    elif 'transport == 'amqp':
        AmqpTransport()
```

### Router

Router determines a way how the reference is look like, how it is awaited,
what data are passed over the network etc. Router is a core of the ATasks package.

The `atasks.router.Router` is a router implementation.

User can initialize the own router implementation.

If you don't, you can use `get_router()` function to get a standard router.


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
    router.activate()
```

## Markup an asynchronous distributed task

Decorator `atasks.tasks.atask` is used to markup the asynchronous coroutine
(or even synchronous returning `future` object).

Note that the first call to the wrapper creates a default router. You should
create your own Router (or ancestor) instance before the first call
to the wrapper.

```python
@atask
async def some task(a):
    ...
```

## Awaiting evaluation of the asynchronous distributed task

Just await an asynchronous remote procedure exactly same as local coroutine.

## Namespaces

All objects may be instantiated in separate namespaces. Just
pass an additional `namespace=...` parameter to:

- constructor of codec, transport, or route object
- atask decorator

One namespace is completely separated from anoher. Every
namespace uses it's own set of routers, transports andd

## Many thanx

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

The `AmqpTransport` allows using RabbitMQ (or analogue) to
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
