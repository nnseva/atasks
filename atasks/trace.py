"""
ATasks call-stack tracing

Builds and carries, across process/network boundaries, the chain of atask
calls (RPC/queue/broadcast) that led to a given atask being executed - and,
optionally, the ordinary ``await`` frames in between - so that an exception
raised anywhere in the chain can be traced back to its root call, regardless
of how many hosts and hops it crossed.

See ``TRACE-ATASK-STACK.md`` for the technical specification this module
implements.
"""

import functools
import importlib.util
import linecache
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


#: contextvar holding the chain of :class:`AtaskHop` (root-first) that led to
#: whatever code is currently running - empty for code not (yet) reached
#: through any atask call.
CURRENT = ContextVar('atasks_trace_current', default=())

#: contextvar holding the live frame of the innermost ``Router._call_coro``
#: currently on the stack (the frame of its ``await coro(...)`` line) - i.e.
#: the boundary between "this atask's own body" and the atasks library/
#: transport/event-loop plumbing that got it called. ``None`` outside of any
#: atask handler (the root call). Set/reset by ``Router._call_coro`` itself;
#: :func:`push_hop` and :func:`attach` use it to keep ordinary-``await``
#: frames scoped to the current hop, instead of reaching back through the
#: machinery of - or past - a previous hop's own execution.
ENTRY_FRAME = ContextVar('atasks_trace_entry_frame', default=None)

#: Marker used to render each ``kind`` of hop in :func:`format_trace`.
_KIND_MARKERS = {
    'rpc': '»RPC«',
    'queue': '»QUEUE«',
    'broadcast': '»BROADCAST«',
}


class AtaskStackTooDeep(RuntimeError):
    """
    Raised by :func:`push_hop` when making a call would exceed the router's
    configured ``max_trace_depth`` - a guard against runaway recursive or
    cyclic atask calls (A calls B calls A ...), which would otherwise grow
    the trace (and the messages carrying it) without bound.
    """


@dataclass(frozen=True)
class FrameInfo:
    """
    A single, plain-data snapshot of one ``await`` frame.

    Deliberately holds only strings/ints (no frame or code objects) so it is
    always picklable and stable across Python versions.
    """

    file: str
    line: int
    func: str
    text: str = ''


@dataclass(frozen=True)
class AtaskHop:
    """
    One atask call in the chain, as seen from the calling side.

    :ivar seq: position from the root, ``0`` is the very first atask call
    :ivar call_id: unique id of this particular call
    :ivar task: name of the atask, as registered
    :ivar namespace: namespace the atask was registered/called in
    :ivar kind: ``'rpc'``, ``'queue'`` or ``'broadcast'``
    :ivar caller_file: file of the call site (``await some_task(...)``)
    :ivar caller_line: line of the call site
    :ivar caller_func: function containing the call site
    :ivar host: identification of the host the call was made from
    :ivar pid: process id the call was made from
    :ivar ts: unix timestamp the call was made at
    :ivar await_frames: ordinary ``await`` frames on the calling side leading
        up to this call (empty if collection is disabled on the router). For
        every hop but the root one, these are scoped to the calling atask's
        own execution (see :data:`ENTRY_FRAME`); for the root hop there is no
        enclosing atask to scope them to, so they reach all the way to the
        top of the stack - whatever that is (a web framework's request
        handler, a plain script, a test runner, ...) - see ``is_root``.
    :ivar is_root: ``True`` if this hop has no atask hop above it - i.e. it
        was called directly, not from within another atask's handler. There
        is exactly one such hop per trace, always at ``seq == 0``.
    """

    seq: int
    call_id: str
    task: str
    namespace: str
    kind: str
    caller_file: str
    caller_line: int
    caller_func: str
    host: str
    pid: int
    ts: float
    await_frames: tuple = field(default_factory=tuple)
    is_root: bool = False


@dataclass(frozen=True)
class AtaskTrace:
    """
    The full, unified trace attached to an exception (as ``__atask_trace__``)
    the first time it is caught anywhere along an atask call chain.

    :ivar hops: chain of :class:`AtaskHop`, root-first, that led to the call
        which ultimately raised
    :ivar raise_host: host the exception was actually raised on
    :ivar raise_pid: process id the exception was actually raised on
    :ivar raise_frames: ordinary ``await`` frames on the raising side, from
        entering the last hop's atask down to the code that raised
    """

    hops: tuple
    raise_host: str
    raise_pid: int
    raise_frames: tuple = field(default_factory=tuple)


def _module_paths(module_name):
    """
    Resolve ``module_name`` to the on-disk path(s) it owns, without importing it.

    For a package, that is its directory (frames of every submodule live under
    it); for a plain module, its single file.

    :param module_name: dotted module name
    :type module_name: str
    :rtype: tuple
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        return ()
    if spec is None:
        return ()
    paths = list(spec.submodule_search_locations or ())
    if spec.origin and spec.origin not in ('built-in', 'frozen'):
        paths.append(spec.origin)
    return tuple(os.path.normpath(p) for p in paths)


@functools.lru_cache(maxsize=None)
def _filter_roots(filter_modules):
    """
    Resolve ``filter_modules`` (a hashable tuple of dotted module names) to path
    prefixes, cached since resolution touches the filesystem/import machinery.

    :param filter_modules: dotted module name prefixes to exclude
    :type filter_modules: tuple
    :rtype: tuple
    """
    roots = []
    for module_name in filter_modules:
        roots.extend(_module_paths(module_name))
    return tuple(roots)


def _is_filtered(filename, filter_modules):
    """
    True if ``filename`` lives under one of ``filter_modules``.

    :param filename: frame filename, as found on a :class:`traceback.FrameSummary`
    :type filename: str
    :param filter_modules: dotted module name prefixes to exclude
    :type filter_modules: tuple
    :rtype: bool
    """
    if not filter_modules:
        return False
    normalized = os.path.normpath(filename)
    for root in _filter_roots(filter_modules):
        if normalized == root or normalized.startswith(root + os.sep):
            return True
    return False


def _frame_info(frame, lineno, filter_modules):
    """
    Snapshot one live frame as a :class:`FrameInfo`, or ``None`` if it is filtered out.

    :param frame: live frame to snapshot
    :type frame: types.FrameType
    :param lineno: line number to record - ``frame.f_lineno`` for a stack frame,
                   or a traceback's ``tb_lineno`` for one taken off a traceback
                   (they can differ for the same frame as it keeps executing)
    :type lineno: int
    :param filter_modules: dotted module name prefixes to exclude
    :type filter_modules: tuple
    :rtype: FrameInfo or None
    """
    filename = frame.f_code.co_filename
    if _is_filtered(filename, filter_modules):
        return None
    text = linecache.getline(filename, lineno, frame.f_globals)
    return FrameInfo(file=filename, line=lineno, func=frame.f_code.co_name, text=text.strip() if text else '')


def _walk_frames_until(frame, boundary, filter_modules):
    """
    Collect ordinary ``await`` frames from ``frame`` upward via ``f_back``, root-first.

    Stops at (and excludes) ``boundary`` - the current hop's own entry point -
    so a hop's frames never reach back into a previous hop's own execution, or
    into the atasks library/transport/event-loop plumbing between the two.
    Walks to the actual top of the stack if ``boundary`` is ``None`` (there is
    no enclosing atask handler - this is the root call).

    :param frame: innermost frame to start from (already excludes the call
                  site itself, which is recorded separately)
    :type frame: types.FrameType or None
    :param boundary: frame to stop at, or ``None``
    :type boundary: types.FrameType or None
    :param filter_modules: dotted module name prefixes to exclude
    :type filter_modules: tuple
    :rtype: tuple
    """
    collected = []
    while frame is not None and frame is not boundary:
        info = _frame_info(frame, frame.f_lineno, filter_modules)
        if info is not None:
            collected.append(info)
        frame = frame.f_back
    collected.reverse()
    return tuple(collected)


def _walk_traceback_after(tb, boundary, filter_modules):
    """
    Collect ordinary ``await`` frames along a traceback, root-first.

    Skips ``boundary`` itself - the current hop's own entry point, i.e.
    ``Router._call_coro``'s ``await coro(...)`` line, which every traceback
    caught there starts at - and everything before it, so only frames that
    are actually part of this hop's own execution are kept.

    :param tb: traceback to walk, e.g. ``exception.__traceback__``
    :type tb: types.TracebackType or None
    :param boundary: frame to skip up to and including, or ``None`` to keep everything
    :type boundary: types.FrameType or None
    :param filter_modules: dotted module name prefixes to exclude
    :type filter_modules: tuple
    :rtype: tuple
    """
    collected = []
    skipping = boundary is not None
    while tb is not None:
        frame = tb.tb_frame
        if skipping:
            if frame is boundary:
                skipping = False
            tb = tb.tb_next
            continue
        info = _frame_info(frame, tb.tb_lineno, filter_modules)
        if info is not None:
            collected.append(info)
        tb = tb.tb_next
    return tuple(collected)


def current():
    """
    Return the chain of hops leading to whatever code is running right now.

    :rtype: tuple
    """
    return CURRENT.get()


def enter(chain):
    """
    Install ``chain`` (as received from the network) as the current chain.

    Must be paired with :func:`leave` (typically via ``try``/``finally``) once
    the call it was received for is done executing.

    :param chain: chain of hops, root-first, as decoded off the wire
    :type chain: tuple
    :returns: token to be passed to :func:`leave`
    """
    return CURRENT.set(tuple(chain or ()))


def leave(token):
    """
    Undo a previous :func:`enter`.

    :param token: token returned by the matching :func:`enter`
    """
    CURRENT.reset(token)


def push_hop(router, name, namespace, kind):
    """
    Build the next hop for a call about to be made, and return the full chain to send.

    Must be called directly from the wrapper making the call (i.e. one frame
    above this function must be the call site to record) - it inspects the
    stack under that assumption.

    :param router: router the call is being made through - supplies host
                   identification and the trace-collection settings
    :type router: atasks.router.Router
    :param name: name of the atask being called
    :type name: str
    :param namespace: namespace of the atask being called
    :type namespace: str
    :param kind: ``'rpc'``, ``'queue'`` or ``'broadcast'``
    :type kind: str
    :returns: the chain (existing chain plus the new hop) to send with the call
    :rtype: tuple
    :raises AtaskStackTooDeep: if appending a hop would exceed ``router.max_trace_depth``
    """
    chain = CURRENT.get()
    if len(chain) >= router.max_trace_depth:
        raise AtaskStackTooDeep(
            'atask call chain exceeded max_trace_depth=%s calling %s/%s' % (router.max_trace_depth, namespace, name)
        )

    caller = sys._getframe(2)  # noqa - the call site above the aioref wrapper that called us
    # None means there is no enclosing atask handler - this call was made
    # directly, so it is the root of its whole chain (always exactly seq 0).
    boundary = ENTRY_FRAME.get()
    await_frames = ()
    if router.collect_await_frames:
        # Everything above the call site itself (already recorded below as
        # caller_file/caller_line/caller_func) up to - but not including -
        # this hop's own entry point, so we never reach back into a previous
        # hop's own execution or into the library/transport/event-loop
        # plumbing in between. For the root hop (boundary is None) there is
        # no such point to stop at, so this reaches all the way to the top of
        # the stack - see AtaskHop.is_root.
        await_frames = _walk_frames_until(caller.f_back, boundary, router.trace_filter_modules)

    hop = AtaskHop(
        seq=len(chain),
        call_id=uuid.uuid4().hex,
        task=name,
        namespace=namespace,
        kind=kind,
        caller_file=caller.f_code.co_filename,
        caller_line=caller.f_lineno,
        caller_func=caller.f_code.co_name,
        host=router.hostname,
        pid=os.getpid(),
        ts=time.time(),
        await_frames=await_frames,
        is_root=boundary is None,
    )
    return chain + (hop,)


def attach(ex, router):
    """
    Attach the accumulated call chain to ``ex`` the first time it is caught.

    A no-op if ``ex`` already carries a trace - it means a deeper hop already
    recorded the actual point of failure, and it must not be overwritten as
    it bubbles back up through intermediate hops.

    :param ex: exception just caught while running an atask
    :type ex: Exception
    :param router: router the atask was called through - supplies host
                   identification and the trace-collection settings
    :type router: atasks.router.Router
    """
    if hasattr(ex, '__atask_trace__'):
        return

    raise_frames = ()
    if router.collect_await_frames and ex.__traceback__ is not None:
        # ex.__traceback__ always starts at Router._call_coro's own
        # `await coro(...)` line - skip it (and, defensively, anything
        # before it) so only frames of this hop's own execution remain.
        raise_frames = _walk_traceback_after(ex.__traceback__, ENTRY_FRAME.get(), router.trace_filter_modules)

    try:
        ex.__atask_trace__ = AtaskTrace(
            hops=CURRENT.get(),
            raise_host=router.hostname,
            raise_pid=os.getpid(),
            raise_frames=raise_frames,
        )
    except AttributeError:
        # Some exception types (e.g. ones defining __slots__) refuse arbitrary
        # attributes - trace collection is best-effort, never fatal.
        logger.warning('Could not attach an atask trace to %r', ex)


def get_trace(ex):
    """
    Return the :class:`AtaskTrace` attached to ``ex``, or ``None``.

    :param ex: exception, possibly carrying a trace attached by :func:`attach`
    :type ex: Exception
    :rtype: AtaskTrace or None
    """
    return getattr(ex, '__atask_trace__', None)


def _format_ts(ts):
    """
    Render a unix timestamp as ``YYYY-MM-DD hh:mm:ss.sssZ``.

    :param ts: unix timestamp
    :type ts: float
    :rtype: str
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z'


def _format_frame(frame, indent):
    """
    Render one :class:`FrameInfo` as a standard-traceback-looking line.

    :param frame: frame to render
    :type frame: FrameInfo
    :param indent: leading spaces
    :type indent: str
    :rtype: str
    """
    line = '%sFile "%s", line %d, in %s' % (indent, frame.file, frame.line, frame.func)
    if frame.text:
        line += '\n%s  %s' % (indent, frame.text.strip())
    return line


def format_trace(ex):
    """
    Render the full atask call chain attached to ``ex`` as human-readable text.

    Looks like a regular traceback, with atask hops picked out by a marker
    for their ``kind`` and annotated with the extra bookkeeping fields saved
    on the hop (call id, task/namespace, host, pid, timestamp).

    :param ex: exception, possibly carrying a trace attached by :func:`attach`
    :type ex: Exception
    :rtype: str
    """
    info = get_trace(ex)
    lines = ['Atask call chain (root -> failure):']
    if info is None:
        lines.append('  <no atask trace attached>')
    else:
        for hop in info.hops:
            # await_frames lead up to this hop's own call site (the marker
            # line right below) - rendered in that actual calling order, the
            # same way a plain traceback puts the outer/earlier frame above
            # the inner/later one it eventually calls into.
            if hop.is_root and hop.await_frames:
                lines.append('  -- entry point (no enclosing atask call) --')
            for frame in hop.await_frames:
                lines.append(_format_frame(frame, '      '))
            marker = _KIND_MARKERS.get(hop.kind, '»ATASK«')
            lines.append(_format_frame(
                FrameInfo(hop.caller_file, hop.caller_line, hop.caller_func), '  %s ' % marker,
            ))
            lines.append(
                '        task=%s namespace=%s kind=%s call_id=%s host=%s pid=%s ts=%s' % (
                    hop.task, hop.namespace, hop.kind, hop.call_id, hop.host, hop.pid, _format_ts(hop.ts),
                )
            )
        for frame in info.raise_frames:
            lines.append(_format_frame(frame, '      '))
        lines.append('  (raised on host=%s pid=%s)' % (info.raise_host, info.raise_pid))
    lines.append('%s: %s' % (ex.__class__.__name__, ex))
    return '\n'.join(lines)
