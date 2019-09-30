# ATasks

ATasks is an asynchronous distributed task queue system.

Every task is defined as an asynchronous coroutine. We call such a task `atask`:
a(synchronous) task.

`atask` looks like a usual asynchronous coroutine. It may be awaited using
`await` syntax, and controlled by the `asyncio` package.

The `atask` may await other coroutines and `atask`s. Because of asynchronous
nature of `atask` it doesn't block a thread evaluating `atask` and
allows easy and transparent task decomposition as usual asynchronous
procedure, including sequential and parallel awaiting of other `atask`s.

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

Before execution some number of core objects should be constructed and initialized.

```python
from atasks.transport.backends.amqp import AMQPTransport
from atasks.router import get_router
from atasks.codecs import PickleCodec

...
    PickleCodec()
    transport = AMQPTransport()
    await transport.connect()

    if mode == 'server':
        router = get_router()
        await router.activate(transport)
```

### Codec

Codec determines a way to encode and decode objects passed through the network.
It should support as many types as it can.

The `atasks.codecs.PickleCodec` provided by the package uses standard python `pickle` package.
It is universal but not always safe solution.

```python
from atasks.codecs import PickleCodec

...
    PickleCodec()
```

User can inherit `atasks.codecs.Codec` as a base class and create an own codec implementation.
Just replace all methods generating `NotImplementedError`. Note that most of methods are asynchronous.
```python
from atasks.codecs import Codec

class MyCodec(Codec):
    async def encode(self, obj):
        ...
    async def decode(self, content):
        ...
```

To activate a codec, yu need just create an instance of it. The codec is installed
into the system while construction.

### Transport

Transport determines the method of sending requests and returning results
from awaiter to the performing coroutine and back to support awaiting
`atask`s among a network.

The `atasks.transport.base.LoopbackTransport` provided by the package passes
all requests back to the awaiter thread only. It doesn't allow `atask`s
performing distribution among several processes or even threads. You can
use it for the testing purposes.

The `atasks.transport.backends.amqp.AMQPTransport` provided by the package passes
requests through the RabbitMQ or other AMQP broker to any ATasks worker started
on the same or another host.

After creation a transport instance, the asynchronous `connect()` method of just
created instance should be awaited.

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

User can inherit `atasks.transport.base.Transport` as a base class and create an own
transport implementation. Just replace all methods generating `NotImplementedError`. Note that
most of methods are asynchronous.

```python
from atasks.transport.base import Transport

class MyTransport(Transport):

    async def connect(self):
        ...

    async def disconnect(self):
        ...

    async def send_request(self, name, content):
        ...
```

### Router

Router determines a way how the reference looks like, how it is awaited,
what data are passed over the network etc. Router is a core of the ATasks package.

The `atasks.router.Router` is an only default router implementation.

User can inherit `atasks.router.Router` and create an own
router implementation if necessary.

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
also activate a transport to receive requests:

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

Client and server should use the same module defining `atask`s as a rule.

In order to await `atask` the `atask` name is used. Default name is determined
by the coroutine name and containing module. You can replace a default name
using additional `name` parameter of the decorator:

```python
@atask(name="some_other_name")
async def some_task(a):
    ...
```

## Awaiting evaluation of the asynchronous distributed task

The `atask` is awaited as a usual coroutine. You can use `await` keyword, or
get a `future` calling `atask` synchronously and control future using `asyncio` module.


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
namespace uses it's own set of router, transport, and codec,
so init them separately for every namespace which is used
in your application.

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
- free combining of `atask` awaits inside another `atask` using `await`
- easy awaiting an `atask` and getting a result
- parallelization using standard asynchronous syntax
- no any restriction for recurrent `await`s

The main advantages of the ATasks comparing with aiotasks:
- easier getting a result (`await` instead of `async with`)
- full transparency - the only difference from usual
  coroutine `await` is distributing `atasks` evaluation
  among a network
- actual development

The main disadvantages comparing with Celery and aiotasks:
- `delay()`, `send()`, `async_call()`, `s()` etc. syntax is not available,
  and will never be implemented

Usual scenarios see in the [scenarios.py](dev/tests/scenarios.py) file.

## Where the atask is evaluated

After the `atask` is started, it is running in one thread from the beginning
to the end. Other `atask`s may share the same thread in an asynchronous manner.

On the other side, another `atask` called from the first one may be
running on any ATasks worker, on the same as the first one, or another
worker and host, depending on the decision taken on the transport layer,
and present ATask workers connected to the same transport layer.

The point where the `atask` is `await`ed is the only point of taking
a decision, where the `await`ed `atask` should run. The
transport layer takes this decision.

The ATasks application can issue remote `await`s immediately after
transport `connect()`. The ATask application receives remote
`await`s after the `activate()` call of the `Router`.

The `LoopbackTransport` always passes all `await`s immediately to
coroutines in the same thread. It may be used for testing purposes.

Other `Transport`s may allow remote `await`s inside a process,
or a host, or passed among a network.

The `AMQPTransport` allows using RabbitMQ (or analogue) to
pass remote `await`s among a network to any number
of instances.

## How to track `atask`

???

## When the `atask` is crashed

The awaiting coroutine will take an exception if the `atask` is crashed
with exception. The exception should be serializable using codec.

## When the worker evaluating `atask` is crashed

???

## How to await `atask` in synchronous program

???
