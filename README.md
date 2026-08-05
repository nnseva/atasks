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

`AMQPTransport` is built entirely on `aio_pika` (never on `pika` or another
AMQP client) - the whole project is expected to standardize on this one
AMQP client library, so `aio_pika` itself should never need to be imported
directly from application code. Notable constructor options:

- `url` - the AMQP broker URL (default `amqp://localhost/`).
- `reconnect_interval` - seconds between reconnection attempts after the
  broker connection is lost (default `5`), passed straight through to
  `aio_pika.connect_robust`.
- `client_properties` - optional dict merged into the AMQP connection
  handshake (e.g. `{'connection_name': 'my-service'}`), useful for
  identifying connections in the broker's management UI/API.
- `prefix` - routing-key/queue/exchange namespacing prefix (default
  `'atask'`) - give distinct services/environments distinct prefixes to keep
  their RPC queues, task-queues, and broadcast exchanges from colliding.

See "Request timeout and combining `@atask` with `backoff`" below for how
`AMQPTransport` surfaces RPC timeouts and connection loss to the caller.

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

### Client and Server

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

Both bare (`@atask`) and parameterized (`@atask(...)`) forms work, and so do
the equivalent forms of `@atask_queue` and `@atask_broadcast` described below.

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

## Request timeout and combining `@atask` with `backoff`

`@atask` accepts an optional `timeout` (seconds). If the worker doesn't reply
in time, the caller gets `atasks.transport.base.RequestTimeoutError` instead
of waiting forever - see "When the worker evaluating `atask` is crashed"
above for the full story, including connection-loss handling.

Because `@atask` and `backoff.on_exception(...)` are both just async-function
decorators, they compose in either order for either purpose. The recommended
shape for a function that runs remotely applies independent retry policies on
each side of the wire:

```python
import backoff
from atasks.tasks import atask
from atasks.transport.base import ConnectionLostError, RequestTimeoutError

@backoff.on_exception(backoff.expo, (RequestTimeoutError, ConnectionLostError))  # retry the whole remote call - caller side
@atask(timeout=30)
@backoff.on_exception(backoff.expo, SomeTransientLocalError)  # retry the local execution - worker side
async def some_processing_function(...):
    ...
    return result
```

- The **worker-side** `backoff.on_exception` retries the underlying function
  locally before ever reporting failure back to the caller - transient
  problems (a flaky downstream HTTP call, a momentary DB hiccup) never even
  cross the wire.
- The **caller-side** `backoff.on_exception` retries the entire remote call -
  including a fresh `correlation_id` and reply-to round trip - when the
  worker-side retries were exhausted, when the worker crashed outright
  (`ConnectionLostError` while a request was in flight, or the same
  `RequestTimeoutError` as a plain timeout, since - as noted above - a
  crashed worker and a slow worker look the same from here).

Decorator order matters: `@atask` must sit directly on the function that
should be registered as (and invoked as) the remote task; a worker-side
`backoff.on_exception` goes *below* it (applied to the plain local coroutine
first), while a caller-side `backoff.on_exception` goes *above* it (applied
to the network-calling stub `@atask` produces).

## Task-queue (fire-and-forget, competing consumers)

Use `@atask_queue` when the caller doesn't need (or want to wait for) a
result, and exactly one instance among however many are currently listening
should handle each call - the classic AMQP work-queue pattern. Good fit for
specialized single-purpose consumer services, e.g. recalculating a rating
when a contract closes, or generating a notification from a tracking event.

```python
from atasks.tasks import atask_queue

@atask_queue
async def recalculate_rating(contract_id):
    ...
```

On the calling side, `await recalculate_rating(contract_id)` publishes the
event and returns `None` immediately - it does not wait for, or receive, any
result.

On the consuming side, a process registers itself as one of the (possibly
several) competing consumers explicitly, since - unlike the RPC pattern's
single `router.activate(transport)` - there can be more than one independent
task-queue (and/or broadcast topic, see below) active in the same process:

```python
from atasks.router import get_router

router = get_router()
await router.activate_queue('mypackage.recalculate_rating', transport)
```

Every instance which calls `activate_queue` with the same name binds to the
*same* durable, named queue - so they compete, and every published event is
delivered to exactly one of them, never to more than one, and never lost even
if published before any consumer has started (the queue is declared durably
by the publisher too).

## Broadcast/subscribe (fire-and-forget, fan-out)

Use `@atask_broadcast` when *every* currently-subscribed instance should
receive and process its own independent copy of each event - the opposite of
`@atask_queue`'s competing-consumers semantics. This is the pattern a fleet
of WebSocket-gateway-style processes needs: every instance holds a different
set of live client connections, and only that instance knows which of them
are relevant to a given event, so every instance must see every event.

```python
from atasks.tasks import atask_broadcast

@atask_broadcast
async def relay_realtime_event(payload):
    ...
```

```python
from atasks.router import get_router

router = get_router()
await router.activate_broadcast('mypackage.relay_realtime_event', transport)
```

Topology: one shared (fanout) exchange per broadcast name, with one
exclusive, auto-delete queue per subscribing instance bound to it - the same
approach used by `channels_rabbitmq`. Each instance gets its own full copy of
the stream while it's connected. Two direct consequences of the exclusive
auto-delete queue:

- a subscriber only receives events published *while it is actively
  subscribed* - there is no replay of history from before it joined (unlike
  the durable queue used by `@atask_queue`, which retains unconsumed events);
- this topology has a known, accepted-for-MVP scaling limitation: every
  subscribed instance receives *every* published event regardless of whether
  it is relevant to any connection that instance actually holds, so
  broker-side + deserialization + filtering load grows linearly with the
  number of subscribed instances, independent of real per-event audience
  size. Sharding by routing key/topic, a connection-presence registry for
  addressed delivery, or broker-side filtering are the directions to
  revisit this if it becomes a bottleneck - not something this package
  solves today.

## Idempotency - at-least-once delivery

**All three patterns - `@atask` (RPC), `@atask_queue` (task-queue), and
`@atask_broadcast` (broadcast) - are at-least-once, never exactly-once.** A
function exposed through any of them can run more than once for what looks
like a single logical event:

- an RPC reply can be lost (network blip, broker restart) after the worker
  already executed successfully, and a caller-side retry (manual, or via
  `backoff.on_exception` as shown above) will then invoke the function again;
- a task-queue or broadcast consumer can crash after processing a message but
  before its ack reaches the broker, so the message (or, for
  auto-acknowledged exclusive queues, a message the consumer never got to
  acknowledge before disconnecting) is redelivered to another consumer (or to
  the same one after it recovers);
- a `RequestTimeoutError`-triggered caller-side retry can race a
  slow-but-still-running worker, resulting in two executions of the same
  logical call.

**This package deliberately does not attempt to solve this for you** - de-
duplication (idempotency keys, "processed event" tables, `INSERT ... ON
CONFLICT DO NOTHING`-style upserts, etc.) is the caller's/handler's
responsibility, exactly as it is for any other at-least-once delivery system
(cloud pub/sub, SQS, periodic cron jobs that might overlap). Every function
registered with `@atask`, `@atask_queue`, or `@atask_broadcast` should be
written to be safely callable more than once with the same logical input.

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

## Commands

The package uses Django management subsystem to provide command-line interface.

Django project using `django_atasks` application has the following command:

```bash
python manage.py run_atask file-or-module [options here]
```

The command runs any `file-or-module` referenced in the command line which contains
`@atask` definitions and optional `aiomain` asynchronous coroutine. The
optional `aiomain` coroutine is evaluated when the file is running.
All options passed to the command are passed then to the `aiomain` keyword parameters.

The `run_atask` management command initializes all necessary objects (as
described above) to run module in three available modes: `server`, `client`,
and `loopback`. The `loopback` mode allows to use the same process instance
as server and client simultaneously.

Note that if you use dedicated `server` process instance, you should not use
`loopback` transport (which is not appropriate to reach the dedicated server
in this case). Use `amqp` (or other interprocess transport) instead.

The module naming is different slightly depending on what you use in command line,
either file name, or module name. Use the same module naming starting server
and client to avoid misnaming of `atask`s.

You can start several modules simultaneously in one process instance
enlisting them all in the command line.

You can start several server process instances, the client will then request them
in arbitrary order.

Call the `help` command to see the command details.

See `dev/tests/scenarios.py` file as an example of the file which can be called
by the `run_atask` management command.

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

Every step of the RPC round trip is logged (`atasks.router` and
`atasks.transport.backends.amqp` loggers) tagged with the request's
`correlation_id`, so grepping a single correlation_id across client and
worker logs reconstructs the whole round trip: request published, request
received, response returning, response received (or the point at which it
stopped - see below). There is no separate tracing/monitoring API beyond
these logs today.

## When the `atask` is crashed

The awaiting coroutine will take an exception if the `atask` is crashed
with exception. The exception should be serializable using codec.

## When the worker evaluating `atask` is crashed

Two distinct failure modes are handled, both without leaving the caller
hanging forever:

- **Request timeout.** Pass `timeout=<seconds>` to `@atask` (or per-decorator
  via `options`) to bound how long the caller waits for a reply:

  ```python
  @atask(timeout=30)
  async def some_task(a):
      ...
  ```

  If no response arrives in time - because the worker crashed mid-task,
  because it was never running in the first place, or because it is simply
  slow - the caller gets `atasks.transport.base.RequestTimeoutError` (a
  subclass of the builtin `TimeoutError`/`asyncio.TimeoutError`). **A crashed
  worker and a slow worker are indistinguishable from the caller's point of
  view** - AMQP gives no signal that a consumer died mid-task, so both
  surface identically once the timeout elapses. Without a `timeout`, the
  historical behaviour is preserved: the caller waits forever.

- **Connection loss.** If the transport's own connection to the broker is
  lost (broker restart, network partition, the whole worker process and its
  connection disappearing, ...), every RPC request currently in flight on
  that connection is failed immediately with
  `atasks.transport.base.ConnectionLostError` (a subclass of the builtin
  `ConnectionError`) - rather than waiting for the configured timeout, or
  hanging past a later reconnect. `AMQPTransport` is built on
  `aio_pika.connect_robust`, so the connection itself keeps retrying (with
  `reconnect_interval`, default 5 seconds) in the background; this exception
  exists purely so an in-flight caller finds out promptly instead of
  discovering it much later.

Both exceptions are ordinary exceptions raised out of the `await`ed call, so
they compose naturally with `backoff.on_exception(...)` wrapped around the
`@atask`-decorated call site - see "Request timeout and combining `@atask`
with `backoff`" above.

## How to await `atask` in synchronous program

There isn't a dedicated synchronous API, and none is planned - `atask` is an
`async def` coroutine like any other, so use it the same way you would use
any other coroutine from synchronous code: `asyncio.run(some_task(...))` (or
`loop.run_until_complete(...)` if you already manage your own loop). See
`atasks/run.py` for exactly this pattern (`aiomain` is invoked via
`loop.run_until_complete`).
