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
        import my_package.tasks  # import every @atask/@atask_queue/@atask_broadcast module first
        router = get_router()
        await router.activate(transport)
```

See [Client and Server](#client-and-server) below for why that import order
matters.

## Namespaces

All objects of the `atasks` package may be instantiated in separate namespaces. The default namespace has a name `"default"`.

Use the `namespace` parameter of any `atasks` class constructor to identiy the namespace where this instance should be instantiated.

The `namespace` parameter may be used in `atask` decorators to identify, in which namespace the `atask` is registered.

The `get_route`, `get_transport`, or `get_codec` functions always return namespace-specific route, transport, and codec instances. Use the `namespace` parameter to select, for which namespace you want to get the instance.

One namespace is completely separated from anoher. Every namespace uses it's own set of router, transport, and codec, so init them separately for every namespace which is used in your application.

You can await atask declared in one namespace, from another.

```python
@atask(namespace='one')
async def some_task():
    await some_other_task()
    ...

@atask(namespace='other')
async def some_other_task():
    ....
```

### Router

Router determines a way how the reference looks like, how it is awaited,
what data are passed over the network, etc. Router is a core of the ATasks package.

The `atasks.router.Router` is an only default router implementation.

#### Router instance

Every namespace has it's own single `Router` instance.

You can create a `Router` instance with non-default constructor parameters if necessary. This should be done *before any call* to other `atasks` parts, including decorators (i.e. even before import of modules which use these decorators).

Creating a Router instance immediately registers this instance in the namespace.

The `Router` constructor parameters are:

- `namespace` - name of the namespace (see [Namespaces](#namespaces)). The default namespace has a name `"default"`.
- `hostname` - name of the host. You can identify your host explicitly to have this name in traces (see [How to track atask](#how-to-track-atask)). The default hostname is provided by the system.
- `max_trace_depth` - maximum trace depth (number of recursive `atask` calls), default is 1000 (see [How to track atask](#how-to-track-atask)). If the trace is deeper, a special exception will be thrown.
- `collect_await_frames` - whether to collect "usual" await frames between `atask` calls in the trace (see [How to track atask](#how-to-track-atask)), default is `True`.  

User can also inherit `atasks.router.Router` and create an own
router implementation if necessary. Create your own instance of the `Router` successor *before any call* to other `atasks` parts, including decorators (i.e. before import of modules which use these decorators).

#### Get the namespace router instance

The `get_router()` function returns the namecpase's `Router` instance.

In many cases, you don't need to create your own `Router` instance. The first `get_router()` call will create the default instance of the `Router` for the correspondent namespace, if the instance is not exist at this time. All other calls of the `get_router()` to the same namespace will return the same instance.

Use the `get_router()` function everywhere the instance of the `Router` is required.

```python
from atasks.router import get_router

...
    router = get_router()
```

Every namespace has it's own `Router` instance. Use the namespace name as an argument to get namespace-specific `Router` instance:

```python
from atasks.router import get_router

...
    router = get_router(namespace='my-specific-namespace')
```

### Codec

Codec determines a way to encode and decode objects passed through the network.
It should support as many types as it can.

#### PickleCodec

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

Every namespace has it's own codec instance. Use the namespace codec constructor parameter to register the codec in the non-default namespace.

### Transport

Transport determines the method of sending requests and returning results
from awaiter to the performing coroutine and back to support awaiting
`atask`s among a network.

The package provides two transport implementations:
- `atasks.transport.base.LoopbackTransport`
- `atasks.transport.backends.amqp.AMQPTransport`

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

#### Loopback Transport

The `atasks.transport.base.LoopbackTransport` provided by the package passes
all requests back to the awaiter thread only. It doesn't allow `atask`s
performing distribution among several processes or even threads. You can
use it for the testing purposes.

#### AMQP Transport

The `atasks.transport.backends.amqp.AMQPTransport` provided by the package passes
requests through the RabbitMQ or other AMQP broker to any ATasks worker started
on the same or another host.

See "Request timeout and combining `@atask` with `backoff`" below for how
`AMQPTransport` surfaces RPC timeouts and connection loss to the caller.

See [AMQP-TRANSPORT-TOPOLOGY.md](AMQP-TRANSPORT-TOPOLOGY.md) for the exact
exchange/queue/routing-key layout `AMQPTransport` creates - useful when
monitoring or administering the broker.

#### Creating your own transport implementation

User can inherit `atasks.transport.base.Transport` as a base class and create an own
transport implementation. Just replace all methods generating `NotImplementedError`. Note that most of methods are asynchronous.

```python
from atasks.transport.base import Transport

class MyTransport(Transport):

    async def connect(self):
        ...

    async def disconnect(self):
        ...

    def is_connected(self):
        ...

    async def send_request(self, name, content):
        ...

    async def publish_event(self, name, content):
        ...

    async def publish_broadcast(self, name, content):
        ...

    async def _register_request_callback(self, name, callback):
        ...

    async def _register_event_callback(self, name, callback):
        ...

    async def _register_broadcast_callback(self, name, callback):
        ...

    async def _unregister_request_callback(self, name):
        ...

    async def _unregister_event_callback(self, name):
        ...

    async def _unregister_broadcast_callback(self, name):
        ...

```

The four `_register_*_callback`/`_unregister_*_callback` pairs are protected -
`Router.activate()`/`deactivate()` are their only caller, once per name known
to the router at the moment `activate()` runs (see [Client and
Server](#client-and-server) below). Library users never call them directly.

### Client and Server

The transport determines, what the role is your application instance plays: client or server.

If your application instance is a client, requesting other instances through the `atask`, you need to only `connect` the transport.

If your application is a server, listening to atask requests, events, and broadcasts, you also need to `activate` the transport on the `Router` instance.

*Client Application*:
```python
    transport = AMQPTransport()
    await transport.connect()
```

*Server Application*:
```python
    transport = AMQPTransport()
    await transport.connect()

    import my_package.tasks  # runs every @atask/@atask_queue/@atask_broadcast decorator

    router = get_router()
    await router.activate(transport)
```

`router.activate(transport)` subscribes, once, to every `@atask`,
`@atask_queue` and `@atask_broadcast` name already registered in this
namespace at the moment it is called - each gets its own subscription (see
[AMQP Transport](#amqp-transport) below for what that looks like on the
wire), never a single catch-all one. Because of that, **every module
containing `@atask`/`@atask_queue`/`@atask_broadcast` decorators this
instance should serve must be imported before `router.activate(transport)` is
called** - not after. Registering a new one afterwards raises
`atasks.router.LateRegistration` instead of being silently ignored, so a
wrong import order fails loudly rather than quietly dropping messages for
whatever was registered too late.

`router.deactivate()` unsubscribes everything the matching `activate()` call
subscribed.

## Markup an asynchronous distributed task

Decorator `atasks.tasks.atask` is used to markup the asynchronous coroutine (or even synchronous returning `future` object) as an asynchronous distributed task.

Note that the first call to the wrapper creates a default router. You should create your own Router (or ancestor) instance before the first call to the wrapper if necessary.

```python
@atask
async def some_task(a):
    ...
```

Client and server should use the same module defining `atask`s as a rule.

In order to await `atask` the `atask` name is used. Default name is determined by the coroutine name and containing module. You can replace a default name using additional `name` parameter of the decorator:

```python
@atask(name="some_other_name")
async def some_task(a):
    ...
```

The `name` influences the network name used to identify the `atask` when it is requested.

Both bare (`@atask`) and parameterized (`@atask(...)`) forms work, and so do the equivalent forms of `@atask_queue` and `@atask_broadcast` described below.

## Awaiting evaluation of the asynchronous distributed task

The `atask` is awaited as a usual coroutine. You can use `await` keyword, or get a `future` calling `atask` synchronously and control future using `asyncio` module.

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

## Request timeout and combining `@atask` with `@backoff`

`@atask` accepts an optional `timeout` (seconds). If the worker doesn't reply
in time, the caller gets `atasks.transport.base.RequestTimeoutError` instead
of waiting forever - see "When the worker evaluating `atask` is crashed"
above for the full story, including connection-loss handling.

Because `@atask` and `@backoff.on_exception(...)` are both just async-function
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

- The **worker-side** `@backoff.on_exception` retries the underlying function
  locally before ever reporting failure back to the caller - transient
  problems (a flaky downstream HTTP call, a momentary DB hiccup) never even
  cross the wire.
- The **caller-side** `@backoff.on_exception` retries the entire remote call -
  including a fresh `correlation_id` and reply-to round trip - when the
  worker-side retries were exhausted, when the worker crashed outright
  (`ConnectionLostError` while a request was in flight, or the same
  `RequestTimeoutError` as a plain timeout, since - as noted above - a
  crashed worker and a slow worker look the same from here).

Decorator order matters: `@atask` must sit directly on the function that
should be registered as (and invoked as) the remote task; a worker-side
`@backoff.on_exception` goes *below* it (applied to the plain local coroutine
first), while a caller-side `@backoff.on_exception` goes *above* it (applied
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
several) competing consumers simply by having imported the module with the
`@atask_queue` decorator before calling `router.activate(transport)` - see
[Client and Server](#client-and-server) above; there is no separate
per-task-queue activation call to make.

Every instance whose `@atask_queue` shares the same name ends up bound to
the *same* durable, named queue - so they compete, and every published event
is delivered to exactly one of them, never to more than one, and never lost
even if published before any consumer has started (the queue is declared
durably by the publisher too).

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

As with `@atask_queue` above, subscribing happens automatically for every
`@atask_broadcast` name that was already registered when `router.activate(transport)`
was called - see [Client and Server](#client-and-server).

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

## Delivery guarantees and idempotency - at-most-once, never exactly-once

**All three patterns - `@atask` (RPC), `@atask_queue` (task-queue), and
`@atask_broadcast` (broadcast) - are at-most-once at the message-delivery
level, not at-least-once.** Every message is acknowledged to the broker as
soon as it is *received*, before the registered handler ever runs - an
architectural constraint, not an oversight: see the comment above
`_on_message` in `atasks/transport/backends/amqp.py` for why deferring the
ack until the handler finishes isn't safe here (a single transport's RPC
consumer shares one AMQP prefetch slot across every task name it serves, and
delaying the ack that long deadlocks on any nested/self-referential call
chain - one task's handler calling another task the same worker also
serves). The practical consequence: **if the process handling a message
crashes, is killed, or loses its connection while the handler is still
running, that message is gone.** AMQP will not redeliver it to another
consumer, and nothing else will ever be told the work didn't happen.

- For RPC (`@atask`), this loss is at least observable from the caller's
  side: `send_request` is still waiting on a reply that will now never
  arrive, so it surfaces as `RequestTimeoutError` (or `ConnectionLostError`,
  if the connection itself drops - see "When the worker evaluating `atask`
  is crashed" below). If the call site follows the documented caller-side
  `backoff.on_exception` pattern, that retry re-issues a brand-new request -
  which can end up running the underlying function twice (if the crashed
  worker had actually finished the work moments before dying, just never got
  to reply) rather than exactly once. This retry is an application-level
  convention this package documents and expects you to add - not something
  AMQP or this library provides automatically.
- For `@atask_queue`/`@atask_broadcast`, there is no caller waiting for
  anything to compare against: `publish_event`/`publish_broadcast` return as
  soon as the message is handed to the broker, with no confirmation that it
  was ever processed. If the consumer that picked it up then crashes
  mid-handler, the work is silently dropped - no retry, no error, no log
  anywhere pointing at it. Anything that must survive a crash mid-processing
  has to be built on top of these two patterns (the handler durably
  recording its own progress/results before returning, an application-level
  dead-letter queue, external monitoring, etc.) - it does not come for free.

**This package deliberately does not attempt to solve either problem for
you.** For RPC, de-duplication (idempotency keys, "processed event" tables,
`INSERT ... ON CONFLICT DO NOTHING`-style upserts, etc.) is the
caller's/handler's responsibility whenever a caller-side retry is in play -
every function registered with `@atask` should be safe to run more than once
for the same logical input. For `@atask_queue`/`@atask_broadcast`, surviving
a crash mid-processing is the handler's own responsibility to design for, if
the use case needs it at all - the delivery mechanism itself won't help.

## Commands

The package provides a command-line interface through the `atasks.run` module.

Run one or more files or Python modules containing `@atask` definitions and an
optional asynchronous `aiomain` coroutine:

```bash
python -m atasks.run file-or-module [file-or-module ...] [options]
```

Each referenced file or module is loaded once, regardless of how many
namespaces it registers `@atask`s into (see [Namespaces](#namespaces)). If it
defines `aiomain`, that coroutine is evaluated; parsed command-line options
(including the parsed `-N`/`--namespace` list, see below) are passed to it as
keyword arguments - see `dev/tests/scenarios.py` for an example.

### Namespaces on the command line

Every namespace the run is meant to touch - even a single one - is configured
with its own `-N`/`--namespace SPEC`, repeatable, one per namespace. `SPEC` is
a comma-separated `key=value` list:

| Key                     | Default    | Meaning |
|--------------------------|------------|---------|
| `name`                   | `default` | Namespace name |
| `mode`                   | `loopback`   | `client` - only connects the transport to send requests/events/broadcasts from the `aiomain` function of the scenario. `server` - additionally activates this namespace's `Router` against its transport, executes `aiomain` and waits to serve requests, `loopback` - connects and activates, but doesn't wait for incoming requests and stops immediately after `aiomain` executed. |
| `transport`              | `loopback` | `loopback` or `amqp` |
| `url`                    | *(none)*   | Passed to the transport, e.g. the broker URL for `transport=amqp` |
| `hostname`               | auto-detected | See the `Router` constructor's `hostname` |
| `max-trace-depth`        | `1000`     | See the `Router` constructor's `max_trace_depth` |
| `trace-filter-modules`   | none filtered | `:`-separated dotted module name prefixes - see the `Router` constructor's `trace_filter_modules` |
| `collect-await-frames`   | `true`     | `true`/`false` - see the `Router` constructor's `collect_await_frames` |

**If any configured namespace is in mode `server`, the process activates every
`server` and `loopback` namespace's `Router` and then blocks,
"Listening for requests"** - regardless of how many
other namespaces are `client` or `loopback`.

The process runs any scenarios' `aiomain()`s to completion. 

Omitting `-N`/`--namespace` entirely is equivalent to a single
`-N name=default` - a namespace named `default` over the loopback
transport in the loopback (default) mode:

```bash
python -m atasks.run dev.tests.scenarios --verbosity 3
```

A `server` mode paired with the `amqp` transport lets one process act
as both server and client for that namespace, and wait for incoming requests:

```bash
python -m atasks.run dev.tests.scenarios -N mode=server,transport=amqp --verbosity 3
```

A `client` mode paired with the `amqp` transport lets one process act
as a pure client, executing it's `aiomain()` function to request server(s):

```bash
python -m atasks.run dev.tests.scenarios -N mode=client,transport=amqp --verbosity 3
```

A `loopback` mode is similar to the `loopback` mode, but doesn't wait for incoming
requests, just executes `aiomain()` and exits:


```bash
python -m atasks.run dev.tests.scenarios -N mode=loopback,transport=amqp --verbosity 3
```

You can use the `loopback` mode to create necessary durable AMQP queues before the service is deployed for the very first time on this AMQP server (see also [AMQP Transport Topology](AMQP-TRANSPORT-TOPOLOGY.md))

Several independent namespaces, each with its own transport/mode, in one
process:

```bash
python -m atasks.run my_scenarios.py \
    -N name=orders,mode=server,transport=amqp,url=amqp://broker/,hostname=worker-1 \
    -N name=billing,mode=client,transport=amqp,url=amqp://broker/
```

Other options, independent of any namespace:

- `-v`, `--verbosity` - logging verbosity from `0` to `4` (default `1`).
- `-L`, `--loggers` - logger names to configure; defaults to `atasks`.
- `-o`, `--option` - additional values made available to `aiomain` as `opt`.

Run the module with `--help` to see the complete command-line reference:

```bash
python -m atasks.run --help
```

Note that if you use a dedicated `server` process instance reached from
another process, you should not use the `loopback` transport for it (it never
reaches outside its own process) - use `amqp` (or another interprocess
transport) instead.

The module naming is different slightly depending on what you use in command line,
either file name, or module name. Use the same module naming starting server
and client to avoid misnaming of `atask`s.

You can start several modules simultaneously in one process instance
enlisting them all in the command line.

You can start several server process instances, the client will then request them
in arbitrary order.

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
stopped - see below).

On top of that, `atasks.trace` builds and carries, across however many `atask`
hops and hosts a call chain crosses, the chain of `atask` calls (RPC/queue/
broadcast) that led to whatever is currently running - and, unless disabled,
the ordinary `await` frames in between. No call arguments are ever recorded,
only call sites (file/line/function), atask/namespace/kind, a host
identification, a per-call id and a timestamp.

Every `atask` call chain starts at the root (the first `atask` called, however
it was called), and is attached to an exception the first time it is caught,
as `exception.__atask_trace__` - fetch it with `atasks.trace.get_trace(exc)`,
or get it pre-rendered as readable text (atask hops picked out from the
ordinary frames) with `atasks.trace.format_trace(exc)`:

```python
from atasks import trace

try:
    await some_task(...)
except Exception as exc:
    info = trace.get_trace(exc)      # AtaskTrace, or None
    print(trace.format_trace(exc))   # human-readable, ready to log
```

- An RPC (`@atask`) failure is routed back to the caller exactly as before,
  with the trace attached, and is **not** logged by the router itself - the
  caller decides whether/how to log it.
- An `atask_queue`/`atask_broadcast` failure has no caller to report back to,
  so it terminates where it happened, logging the full collected trace instead
  of a bare exception `repr()`.

Host identification, the call-depth guard, and whether/what to filter out of
the ordinary-`await` frames are all set on `Router`, and must be set *before*
the first direct or indirect call to `get_router()` for the namespace - after
that, `get_router()` returns the instance you constructed:

```python
from atasks.router import Router

Router(
    hostname='worker-fleet-2',       # default: socket.gethostname()
    max_trace_depth=1000,            # atask hops only; guards against runaway recursion/cycles
    trace_filter_modules=('atasks', 'backoff'),  # default: none filtered
    collect_await_frames=True,       # False: trace holds only atask hops, no ordinary frames
)
```

`run.py` exposes these as the `hostname`, `max-trace-depth`, `trace-filter-modules`
and `collect-await-frames` keys of its per-namespace `-N`/`--namespace SPEC` -
see [Commands](#commands).

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
