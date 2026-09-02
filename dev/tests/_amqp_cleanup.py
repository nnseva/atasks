"""
dev/tests-only AMQP broker hygiene helper.

``AMQPTransport`` deliberately leaves its durable per-name queues
(``_request_queues`` for ``@atask``, ``_event_queues`` for ``@atask_queue``)
in place across ``disconnect()``/``Router.deactivate()`` - a durable queue's
whole point is to survive exactly that, so another (or a reconnecting)
instance keeps competing for whatever is still on it. See
``AMQP-TRANSPORT-TOPOLOGY.md`` for why that's the right behaviour for a real
deployment.

Tests have no such need: each test declares its own uniquely-namespaced
(``prefix=``/``queue=`` set to a fresh uuid), throwaway queues and never
comes back for them - left alone, they simply accumulate on the broker
forever. :func:`teardown_amqp` deletes whatever a test's transports still
know about before disconnecting them.
"""
import logging

import aio_pika


logger = logging.getLogger(__name__)


async def _delete_queues(queue_names, url):
    """
    Delete ``queue_names`` (already-existing durable queues, by name) on
    ``url``, over one connection unrelated to whatever transport(s)
    originally declared them.

    Deliberately never reuses the original transport's own connection/
    channel, even if it still looks alive: that connection is an
    ``aio_pika`` *robust* one, which self-heals in the background and, on
    reconnecting, redeclares/rebinds/re-consumes everything it had
    previously declared - including the very queue being deleted here. A
    fresh, disposable connection has no such memory. Callers should
    therefore make sure the owning transport is already disconnected (a
    deliberate ``disconnect()``, unlike a transient drop, is *not* something
    aio_pika tries to heal - see the module docstring in
    ``test_010_amqp_reconnect.py``) before calling this, so there is no
    window left for it to resurrect what's being deleted here.
    """
    if not queue_names or url is None:
        return
    try:
        connection = await aio_pika.connect_robust(url)
    except Exception:
        logger.debug('Could not open a cleanup connection to %s during test cleanup', url, exc_info=True)
        return
    try:
        channel = await connection.channel()
        for name in queue_names:
            try:
                # Passive: the queue must already exist - never created here.
                queue = await channel.declare_queue(name, durable=True, passive=True)
                # if_unused/if_empty default to True on aio_pika's Queue.delete() -
                # test cleanup wants it gone, full stop, consumers or not.
                await queue.delete(if_unused=False, if_empty=False)
            except Exception:
                # Best-effort: a queue another still-active transport also holds a
                # reference to (already deleted by it), or one the broker already
                # dropped for its own reasons, must not fail the test.
                logger.debug('Could not delete queue %r during test cleanup', name, exc_info=True)
    finally:
        await connection.close()


async def teardown_amqp(router, transports):
    """
    Delete every durable queue still tracked by ``transports``, deactivate
    ``router`` (if given), then disconnect every transport - in that order:
    disconnecting *before* deleting would leave a small window where an
    already-dying connection's robust auto-reconnect finishes and redeclares
    the very queue this just deleted (see :func:`_delete_queues`), so every
    transport is disconnected first here, and deletion always goes through
    a separate, disposable connection afterwards.

    Safe to call more than once, with transports already disconnected, or
    with a ``router`` already deactivated - everything here is best-effort.

    :param router: the ``Router`` to deactivate, or ``None`` to skip that step
                   (e.g. when the test drove the transport's per-name
                   registration methods directly, never through a ``Router``)
    :type router: atasks.router.Router or None
    :param transports: transports whose durable queues should be cleaned up;
                       ``None`` entries are ignored, so passing e.g.
                       ``[self.client_transport, self.server_transport]``
                       straight through is fine
    :type transports: list
    """
    transports = [t for t in transports if t is not None]
    # queue names grouped by the url of the transport that declared them,
    # so all of one transport's queues are cleaned up over a single shared
    # disposable connection.
    by_url = {}
    for transport in transports:
        url = getattr(transport, 'url', None)
        if url is None:
            continue
        names = [q.name for q in getattr(transport, '_request_queues', {}).values()]
        names += [q.name for q in getattr(transport, '_event_queues', {}).values()]
        if names:
            by_url.setdefault(url, []).extend(names)

    if router is not None:
        try:
            await router.deactivate()
        except Exception:
            logger.debug('router.deactivate() failed during test cleanup', exc_info=True)

    for transport in transports:
        try:
            await transport.disconnect()
        except Exception:
            logger.debug('transport.disconnect() failed during test cleanup', exc_info=True)

    for url, names in by_url.items():
        await _delete_queues(names, url)
