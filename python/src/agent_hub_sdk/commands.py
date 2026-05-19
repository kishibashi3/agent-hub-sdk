"""Command routing for ``/<verb>`` messages.

Generalises the M2 hardcoded ``/ping`` intercept into a user-extensible
dispatch table. Aligns with the ``/<verb>`` machine-to-machine convention
in `agent-hub#92 <https://github.com/kishibashi3/agent-hub/issues/92>`_.

Design lives at `agent-hub-sdk#10
<https://github.com/kishibashi3/agent-hub-sdk/issues/10>`_ — Revisions 1
and 2 are the surviving plan.

Public surface
==============

- :class:`CommandRouter` — register handlers and passthroughs; pass to
  :meth:`agent_hub_sdk.HubSession.inbox` as ``commands=router``.
- :func:`parse_command` — split a message body into ``(cmd, args)``; the
  same parser the router uses, exposed so bridges that opt out of the
  router can still get consistent semantics.

Built-in handlers
=================

A default ``CommandRouter()`` registers three universal protocol commands
(disable with ``CommandRouter(builtins=False)``):

- ``/ping`` → ``pong`` (plain text). Inbox-listener health check.
- ``/status`` → the value set by :meth:`HubSession.set_status` (default
  ``"idle"``). Bridges update this from their work loop.
- ``/help`` → auto-generated list of registered commands + descriptions.

Unknown ``/foo`` (no handler, no passthrough) is replied to with the
plain text ``command not found: /foo`` and ``ack``'d, unless the router
is configured with ``unknown="yield"`` to pass it through to the
consumer instead. See agent-hub-sdk#10 Revision 2 for the rationale on
the plain-text reply form.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_hub_sdk.messages import IncomingMessage
    from agent_hub_sdk.session import HubSession

logger = logging.getLogger(__name__)

__all__ = [
    "CommandHandler",
    "CommandRouter",
    "DispatchResult",
    "parse_command",
]


#: Handler signature: ``async def handler(msg, hub, args) -> str | None``.
#:
#: - ``msg`` — the full :class:`IncomingMessage`.
#: - ``hub`` — the live :class:`HubSession` (= so handlers can ``send``,
#:   ``get_participants``, etc. mid-handler).
#: - ``args`` — text after the command token, ``str.strip``\\ ed. Empty
#:   string when no args. Handlers parse this themselves (the SDK does
#:   not impose an argument grammar).
#: - return ``str`` → SDK sends as reply to ``msg.sender``, then acks.
#: - return ``None`` → SDK does **not** auto-reply (handler did its own
#:   sending or chose silence); SDK still acks.
#: - raise → SDK logs + sends a generic ``:warning: command <cmd> failed:
#:   <err>`` to the sender, then acks. The exception does **not**
#:   propagate to the inbox iterator.
CommandHandler = Callable[
    ["IncomingMessage", "HubSession", str],
    Awaitable[str | None],
]

#: Result of :meth:`CommandRouter.dispatch`. ``"handled"`` means the
#: router handled the message (= the inbox iterator should NOT yield it
#: to the consumer). ``"yield"`` means the inbox iterator SHOULD yield
#: it (= passthrough, non-command, or ``unknown="yield"`` mode).
DispatchResult = Literal["handled", "yield"]


# Plain-text reply for the built-in /ping. agent-hub-sdk#10 Revision 2
# settled on plain text rather than the command-format ``/pong``: no peer
# parses ``/pong``, and a plain string is cheaper for receivers (log it,
# match it, done). Bridges that want the command-format reply override
# ``/ping`` with their own handler that returns ``"/pong"``.
_PING_REPLY = "pong"

# Plain-text rejection format for unknown commands. ``{cmd}`` is the
# parsed command token (with leading slash). agent-hub-sdk#10 Revision 2
# again — receivers benefit from a readable error string, not a
# ``/unknown <cmd>`` protocol response to parse.
_DEFAULT_REJECT_FORMAT = "command not found: {cmd}"

# Built-in command tokens and their default ``/help`` descriptions.
# Override semantics: if a user registers a handler for one of these
# tokens via ``@router.command(...)``, that handler wins. The default
# description is also overridden by the registered one.
_BUILTIN_NAMES: tuple[str, ...] = ("/ping", "/status", "/help")
_BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "/ping": "health check",
    "/status": "bridge state (idle/busy/rate-limited)",
    "/help": "show this help",
}

# Default bridge status when ``HubSession.set_status`` has not been
# called. Bridges that don't care about ``/status`` can leave the value
# alone; operators reading it see ``idle`` and know the bridge accepted
# the protocol but doesn't expose work-state.
DEFAULT_STATUS = "idle"


def parse_command(text: str | None) -> tuple[str | None, str]:
    """Split a message body into ``(command, args_text)``.

    A command is the first whitespace-delimited token of a body whose
    first non-whitespace character is ``/``. The bare ``/`` is not a
    command (returns ``(None, "")``).

    :param text: the raw message body, or ``None``.
    :returns: ``(cmd, args)``. ``cmd`` is ``None`` for non-command bodies;
        ``args`` is empty string when no args are present.

    >>> parse_command("/list verbose")
    ('/list', 'verbose')
    >>> parse_command("  /ping  ")
    ('/ping', '')
    >>> parse_command("hello")
    (None, '')
    >>> parse_command("/")
    (None, '')
    >>> parse_command("")
    (None, '')
    >>> parse_command(None)
    (None, '')
    """
    if not text:
        return None, ""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, ""
    parts = stripped.split(maxsplit=1)
    cmd = parts[0]
    if cmd == "/":
        return None, ""
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


class CommandRouter:
    """Dispatch table for ``/`` prefix command messages.

    Construct, register handlers / passthroughs, then pass to
    :meth:`agent_hub_sdk.HubSession.inbox` as ``commands=router``::

        router = CommandRouter()

        @router.command("/active", description="current task")
        async def handle_active(msg, hub, args):
            return f"working on {current_task()}"

        @router.command("/list", description="list jobs (optional: verbose)")
        async def handle_list(msg, hub, args):
            return format_jobs(await db.list(verbose=args == "verbose"))

        # /help is built-in; /explain is a known command but we want the
        # LLM to answer it, so passthrough.
        router.passthrough("/explain", description="ask the LLM")

        async with hub.inbox(commands=router) as messages:
            async for msg in messages:
                # /active /list /ping /status /help → never reach here
                # /explain → reaches here (= passthrough)
                # /foo (unknown) → SDK auto-replied "command not found"
                # natural language → reaches here
                ...

    :param builtins: when ``True`` (default), the built-in handlers for
        ``/ping``, ``/status``, ``/help`` are registered with their
        default behaviour. User-registered handlers always win over the
        built-in for the same command name.
    :param unknown: behaviour for ``/foo`` with no handler and no
        passthrough. ``"reject"`` (default) → SDK auto-replies with
        ``reject_format`` and acks. ``"yield"`` → SDK yields the message
        to the consumer just like a non-command body.
    :param reject_format: Python ``str.format`` template for the auto-
        reject reply. Default is ``"command not found: {cmd}"``. The
        ``{cmd}`` placeholder is filled with the parsed command token
        (including leading slash).
    """

    def __init__(
        self,
        *,
        builtins: bool = True,
        unknown: Literal["yield", "reject"] = "reject",
        reject_format: str = _DEFAULT_REJECT_FORMAT,
    ) -> None:
        self._handlers: dict[str, CommandHandler] = {}
        self._descriptions: dict[str, str] = {}
        self._passthrough: set[str] = set()
        self._builtins_enabled = builtins
        self._unknown_mode = unknown
        self._reject_format = reject_format

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def command(
        self,
        path: str,
        *,
        description: str | None = None,
    ) -> Callable[[CommandHandler], CommandHandler]:
        """Decorator that registers ``fn`` as the handler for ``path``.

        Usage::

            @router.command("/active", description="current task")
            async def handle_active(msg, hub, args) -> str | None:
                return f"working on {current_task()}"

        ``path`` must start with ``/``. Registering the same path twice
        overwrites the previous handler (= explicit override). To
        override a built-in (e.g. provide a custom ``/help`` formatter),
        just register a handler for it — the user's handler wins.
        """
        if not path.startswith("/"):
            raise ValueError(
                f"command path must start with '/' (got {path!r})"
            )

        def decorator(fn: CommandHandler) -> CommandHandler:
            self._handlers[path] = fn
            if description is not None:
                self._descriptions[path] = description
            return fn

        return decorator

    def passthrough(self, path: str, *, description: str | None = None) -> None:
        """Mark ``path`` as a known command that the SDK should yield.

        Useful when the consumer (e.g. an LLM) should answer the command
        but you still want it to show up in ``/help`` and be excluded
        from the ``command not found`` rejection path.

        ``path`` must start with ``/``. If a handler is already
        registered for the same path, the handler wins (= passthrough
        is ignored). Registering a passthrough twice is a no-op.
        """
        if not path.startswith("/"):
            raise ValueError(
                f"passthrough path must start with '/' (got {path!r})"
            )
        self._passthrough.add(path)
        if description is not None:
            self._descriptions[path] = description

    # ------------------------------------------------------------------
    # Dispatch (= called by HubSession.inbox internals)
    # ------------------------------------------------------------------

    async def dispatch(
        self, msg: IncomingMessage, hub: HubSession
    ) -> DispatchResult:
        """Route one inbox message. Returns whether to yield to the consumer.

        Precedence:

        1. Non-command body → ``"yield"`` (= ordinary message).
        2. User-registered handler for the command token → run it, return
           ``"handled"``.
        3. Built-in handler (if ``builtins=True``) → run it, return
           ``"handled"``.
        4. Passthrough → ``"yield"``.
        5. Unknown — depends on ``unknown`` mode: ``"reject"`` auto-
           replies with ``reject_format`` and returns ``"handled"``;
           ``"yield"`` returns ``"yield"``.

        Side-effects: for ``"handled"`` returns, the message is **also**
        acked. The consumer does not see the message and does not need
        to ack it. For ``"yield"`` returns, the consumer receives the
        message and is responsible for acking.
        """
        cmd, args = parse_command(msg.body)
        if cmd is None:
            return "yield"

        # 1. User handler wins
        handler = self._handlers.get(cmd)
        if handler is not None:
            await self._run_handler(handler, msg, hub, cmd, args)
            return "handled"

        # 2. Built-in handler (no user override)
        if self._builtins_enabled:
            builtin = _BUILTIN_HANDLERS.get(cmd)
            if builtin is not None:
                # Built-in handlers receive the router as an extra arg
                # so /help can introspect registered commands.
                await self._run_builtin(builtin, msg, hub, cmd, args)
                return "handled"

        # 3. Passthrough — known command, defer to consumer
        if cmd in self._passthrough:
            return "yield"

        # 4. Unknown
        if self._unknown_mode == "reject":
            await self._reject_unknown(msg, hub, cmd)
            return "handled"
        return "yield"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_handler(
        self,
        handler: CommandHandler,
        msg: IncomingMessage,
        hub: HubSession,
        cmd: str,
        args: str,
    ) -> None:
        """Invoke a user-registered handler and act on its result.

        - ``str`` return → send as reply.
        - ``None`` return → no auto-reply (handler did its own work).
        - Exception → log + send a generic warning reply. Never
          propagates to the inbox iterator (= one bad handler doesn't
          take down the listener).
        Ack is issued regardless (the message was consumed).
        """
        try:
            result = await handler(msg, hub, args)
        except Exception:
            logger.exception("command %s handler failed", cmd)
            try:
                await hub.send(
                    msg.sender,
                    f":warning: command {cmd} failed (see bridge log)",
                )
            except Exception:
                logger.exception(
                    "could not send error reply for %s", cmd
                )
        else:
            if isinstance(result, str):
                try:
                    await hub.send(msg.sender, result)
                except Exception:
                    logger.exception(
                        "could not send reply for %s", cmd
                    )

        try:
            await hub.ack(msg.id)
        except Exception:
            logger.exception("could not ack %s after handler", msg.id)

    async def _run_builtin(
        self,
        builtin: _BuiltinHandler,
        msg: IncomingMessage,
        hub: HubSession,
        cmd: str,
        args: str,
    ) -> None:
        """Invoke a built-in handler (needs the router for /help)."""
        try:
            result = await builtin(self, msg, hub, args)
        except Exception:
            logger.exception("built-in %s failed", cmd)
            try:
                await hub.send(
                    msg.sender,
                    f":warning: built-in {cmd} failed (see bridge log)",
                )
            except Exception:
                logger.exception(
                    "could not send error reply for built-in %s", cmd
                )
        else:
            if isinstance(result, str):
                try:
                    await hub.send(msg.sender, result)
                except Exception:
                    logger.exception(
                        "could not send reply for built-in %s", cmd
                    )

        try:
            await hub.ack(msg.id)
        except Exception:
            logger.exception(
                "could not ack %s after built-in", msg.id
            )

    async def _reject_unknown(
        self, msg: IncomingMessage, hub: HubSession, cmd: str
    ) -> None:
        """Auto-reply for ``unknown="reject"`` mode."""
        logger.info("[command] from=%s unknown=%s", msg.sender, cmd)
        reply = self._reject_format.format(cmd=cmd)
        try:
            await hub.send(msg.sender, reply)
        except Exception:
            logger.exception("could not send reject reply for %s", cmd)
        try:
            await hub.ack(msg.id)
        except Exception:
            logger.exception("could not ack %s after reject", msg.id)

    def _generate_help_text(self) -> str:
        """Build the ``/help`` reply from registered commands + built-ins.

        Order: built-ins (in declared order, with override descriptions
        applied), then user-registered commands (in registration order),
        then passthroughs (in registration order).

        Format: ``<cmd-padded-to-12> — <description>``. Padding is just
        for alignment in monospace; the receiver can re-parse if needed.
        """
        lines: list[str] = []

        # Track which command names we've already emitted so user
        # handlers that override a built-in don't get listed twice.
        emitted: set[str] = set()

        # 1. Built-ins (if enabled), in declared order.
        if self._builtins_enabled:
            for name in _BUILTIN_NAMES:
                if name in emitted:
                    continue
                # If user overrode this built-in, prefer their handler
                # listing later in the registration-order block; skip
                # the built-in entry here.
                if name in self._handlers:
                    continue
                desc = self._descriptions.get(
                    name, _BUILTIN_DESCRIPTIONS.get(name, "")
                )
                lines.append(f"{name:12} — {desc}")
                emitted.add(name)

        # 2. User handlers, in registration (dict insertion) order.
        for name, _fn in self._handlers.items():
            if name in emitted:
                continue
            desc = self._descriptions.get(
                name, _BUILTIN_DESCRIPTIONS.get(name, "")
            )
            lines.append(f"{name:12} — {desc}")
            emitted.add(name)

        # 3. Passthroughs, in registration order (sorted for determinism
        #    since ``set`` doesn't preserve insertion order).
        for name in sorted(self._passthrough):
            if name in emitted:
                continue
            desc = self._descriptions.get(name, "")
            lines.append(f"{name:12} — {desc}")
            emitted.add(name)

        if not lines:
            # Edge case: builtins=False and no user handlers/passthroughs.
            return "(no commands registered)"
        return "\n".join(lines)


# ---------------------------------------------------------------------
# Built-in handler implementations
# ---------------------------------------------------------------------
#
# Signature differs from CommandHandler: built-ins need the router so
# /help can introspect it. They take (router, msg, hub, args).


_BuiltinHandler = Callable[
    ["CommandRouter", "IncomingMessage", "HubSession", str],
    Awaitable[str | None],
]


async def _builtin_ping(
    _router: CommandRouter,
    _msg: IncomingMessage,
    _hub: HubSession,
    _args: str,
) -> str:
    """Universal health check. Returns the plain string ``"pong"``."""
    logger.info("[command] /ping → pong (from=%s)", _msg.sender)
    return _PING_REPLY


async def _builtin_status(
    _router: CommandRouter,
    _msg: IncomingMessage,
    hub: HubSession,
    _args: str,
) -> str:
    """Bridge state report. Reads ``hub._status`` (default ``"idle"``)."""
    # ``HubSession.set_status`` writes to ``hub._status``; the attribute
    # is private to the SDK but stable within the SDK's own modules.
    return getattr(hub, "_status", DEFAULT_STATUS)


async def _builtin_help(
    router: CommandRouter,
    _msg: IncomingMessage,
    _hub: HubSession,
    _args: str,
) -> str:
    """Auto-generated command list. See :meth:`CommandRouter._generate_help_text`."""
    return router._generate_help_text()


_BUILTIN_HANDLERS: dict[str, _BuiltinHandler] = {
    "/ping": _builtin_ping,
    "/status": _builtin_status,
    "/help": _builtin_help,
}
