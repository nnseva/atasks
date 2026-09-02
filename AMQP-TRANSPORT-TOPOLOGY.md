# AMQPTransport topology

Reference for whoever monitors or administers the AMQP broker
`atasks.transport.backends.amqp.AMQPTransport` runs against - exchanges,
queues, routing keys, and what to expect to see change in the broker's
management UI/API. Not needed to just *use* the library - see the main
[README.md](README.md) for that; this document is specific to the AMQP
backend, not to `Router`/`Transport` in general.

## Constructor parameters that shape the topology

- `prefix` (default `'atask'`) - routing-key namespacing prefix. Every
  message this transport publishes or binds to carries `<prefix>.` followed
  by a one-letter pattern marker (`r`/`e`/`b`, see below).
- `queue` (default `'atask'`) - queue-naming prefix, combined with `prefix`
  to name every durable queue this transport declares.
- `request_exchange`, `response_exchange`, `event_exchange`,
  `broadcast_exchange` (all default `'atask'`) - exchange names. They may all
  resolve to the same physical exchange (the default) or be split apart; it's
  the routing-key namespace, not exchange identity, that keeps the four kinds
  of traffic apart. All four are declared as durable topic exchanges.

Give a service/environment its own `prefix`/`queue` values to keep its
queues and routing keys from colliding with another service/environment
sharing the same broker.

## Exchanges

| exchange (constructor param) | type | durable | default name |
|---|---|---|---|
| `request_exchange` | topic | yes | `atask` |
| `response_exchange` | topic | yes | `atask` |
| `event_exchange` | topic | yes | `atask` |
| `broadcast_exchange` | topic | yes | `atask` |

## Queues

| pattern | one queue per | name | durable | exclusive | auto-delete | bound routing key |
|---|---|---|---|---|---|---|
| RPC request (`@atask`) | registered atask **name** | `<queue>.<prefix>.r.<name>` | yes | no | no | `<prefix>.r.<name>` (exact) |
| RPC response | transport **instance** | broker-generated (anonymous) | no | yes | (implied by exclusive) | own queue name |
| task-queue event (`@atask_queue`) | registered task **name** | `<queue>.<prefix>.q.<name>` | yes | no | no | `<prefix>.e.<name>` (exact) |
| broadcast (`@atask_broadcast`) | subscribing **instance**, per topic | broker-generated (anonymous) | no | yes | yes | `<prefix>.b.<name>` (exact) |

Two different growth axes to keep in mind:

- **RPC and task-queue queues grow with the number of distinct registered
  names**, not with the number of instances or hosts - every instance
  registering the same `@atask`/`@atask_queue` name shares that one queue and
  competes for its messages. This is deliberate: it's what lets a
  heterogeneous fleet (different hosts registering different subsets of
  `@atask`s) receive each request only where a handler for it actually
  exists, instead of it being picked up by an instance that would just raise
  `JobNotFound` and drop the message.
- **RPC response and broadcast queues grow with the number of connected
  instances** - each gets its own anonymous, exclusive queue, cleaned up by
  the broker itself when that connection closes.

No routing key here is ever a mask (`#`/`*`) - every binding above is for the
exact key shown. Compare with the previous single-mask-bound RPC queue this
replaced (see `ATASK-NEW-ARCHITECTURE-PLAN.md` in the repository history for
the rationale).

## What changes in the broker's management UI

Instead of one RPC queue (named `atask` by default) shared by every instance
in a namespace, expect to see one RPC queue **per distinct registered
`@atask` name** (e.g. `atask.atask.r.mypackage.some_task`), and likewise one
task-queue queue per distinct registered `@atask_queue` name. A deployment
with many distinct atask names will show correspondingly many queues - this
is expected, not a leak; each is durable and stable for as long as at least
one instance keeps that name registered.

Anonymous exclusive queues (RPC responses, broadcast subscriptions) show up
with broker-generated names and disappear automatically when the owning
connection closes - nothing to clean up manually there.
