"""Installing the guard: httpx's and httpx2's transports, a CPython audit hook, and the process calls it cannot see.

Both libraries' transports (`hajer._http_libraries`: httpx, and httpx2 when the interpreter has it — the library
openai 3.x and anthropic 1.x send through) are routed through the one sandbox with the same semantics. A request
through any other client reaches the socket layer, where the audit hook below is the backstop and refuses it.

The audit hook (`sys.addaudithook`) cannot be removed once added and runs for the C-level operation, so it
decides for `socket.socket`, `_socket.socket` and a `connect` taken off the base class alike:

* `socket.connect` (`Sandbox.connecting`) reaches only an endpoint's tunnel while the guard is sending on it, or, in
  a live CI run with no tunnel, one of the model provider's own addresses at its exact port while the guard sends to
  it directly — so a proxy, a Unix socket or a transport's own socket elsewhere is refused even mid-send. This holds
  for sockets connected through CPython's `socket` module, where the event fires: a C-level stack beside it (uvloop,
  grpcio, pycurl) connects unseen, and in-process tampering with the guard is not resisted (`_guard`'s known limits);
  a declared direct provider's own name may be looked up from any thread (`Sandbox.resolvable`), because an async
  client resolves it in the event loop's executor, outside the sending task's context;
  every other address, TCP or Unix, is BLOCKED_EFFECT. `socket.sendto`/`sendmsg` to an address likewise;
* a name lookup (`getaddrinfo`, `gethostbyname[_ex]`, `gethostbyaddr`, `getnameinfo`) is NETWORK_UNRECORDED
  unless the host is already a numeric address — the child needs no names, its tunnels are paths;
* `subprocess.Popen`, `os.system`, `os.posix_spawn[p]`, `os.spawn*`, `os.exec*`, `os.fork` and `os.forkpty`
  are BLOCKED_EFFECT: a child process would be a second interpreter without this guard.

Three process paths raise no audit event, so they are replaced where they live: `_posixsubprocess.fork_exec`
(what `multiprocessing`'s spawn and forkserver call), `multiprocessing`'s `BaseProcess.start`, and its
`SemLock` — the semaphore every `Queue`, `Pool` and `ProcessPoolExecutor` builds before it starts a worker
(and which the operating-system sandbox refuses anyway, so without this the refusal would go unrecorded). And `ctypes` can call the C library's `connect` or `fork` beneath
every Python-level hook, so looking one of those symbols up (`ctypes.dlsym`) is BLOCKED_EFFECT too — the
operating system would refuse the call itself, and this is how the attempt learns it was tried.
"""

from __future__ import annotations

import _posixsubprocess
import contextlib
import importlib
import multiprocessing.process
import socket
import sys

import httpx

from hajer._http_libraries import HttpLibrary, libraries
from hajer.replay._guard import AsyncSend, Sandbox, SyncSend, address_label, numeric

_PROCESS_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.forkpty"}
)
_NAME_EVENTS = frozenset({"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"})
_SEND_EVENTS = frozenset({"socket.sendto", "socket.sendmsg"})
_NATIVE_EFFECTS = frozenset(
    {
        "connect",
        "sendto",
        "sendmsg",
        "getaddrinfo",
        "gethostbyname",
        "fork",
        "vfork",
        "execve",
        "execv",
        "execvp",
        "posix_spawn",
        "posix_spawnp",
        "system",
        "popen",
    }
)


def _audit(sandbox: Sandbox) -> None:
    def hook(event: str, args: tuple[object, ...]) -> None:
        if event == "socket.connect":
            refused = sandbox.connecting(args[0], args[1])
            if refused is not None:
                raise ConnectionRefusedError(refused)
        elif event in _SEND_EVENTS:
            if len(args) > 1 and args[1] is not None:
                host, port = address_label(args[1])
                sandbox.refuse("BLOCKED_EFFECT", host, port)
                raise ConnectionRefusedError("HAJER_REPLAY: datagram refused in the sandbox")
        elif event in _NAME_EVENTS:
            name = args[0] if args else None
            text = name.decode(errors="replace") if isinstance(name, bytes) else name
            allowed = isinstance(text, str) and sandbox.resolvable(text)
            if not allowed and (event != "socket.getaddrinfo" or (isinstance(text, str) and not numeric(text))):
                sandbox.refuse("NETWORK_UNRECORDED", f"{text}", 0)
                raise socket.gaierror(socket.EAI_NONAME, "HAJER_REPLAY: name resolution refused in the sandbox")
        elif event in _PROCESS_EVENTS:
            sandbox.refuse("BLOCKED_EFFECT", "process", 0)
            raise PermissionError("HAJER_REPLAY: starting a process is refused in the sandbox")
        elif event == "ctypes.dlsym" and len(args) > 1 and args[1] in _NATIVE_EFFECTS:
            sandbox.refuse("BLOCKED_EFFECT", f"native:{args[1]}", 0)
            raise PermissionError("HAJER_REPLAY: a native network or process call is refused in the sandbox")

    sys.addaudithook(hook)


def _processes(sandbox: Sandbox) -> None:
    def refused(*_args: object, **_kwargs: object) -> None:
        sandbox.refuse("BLOCKED_EFFECT", "process", 0)
        raise PermissionError("HAJER_REPLAY: starting a process is refused in the sandbox")

    setattr(_posixsubprocess, "fork_exec", refused)  # noqa: B010
    setattr(multiprocessing.process.BaseProcess, "start", refused)  # noqa: B010
    with contextlib.suppress(ImportError):  # a platform without POSIX semaphores has no SemLock to refuse
        synchronize = importlib.import_module("multiprocessing.synchronize")
        setattr(synchronize.SemLock, "__init__", refused)  # noqa: B010


def _transports(sandbox: Sandbox, library: HttpLibrary) -> None:
    """Every request `library`'s own transports are asked to send goes to the sandbox instead, tagged with it."""
    async_inner: AsyncSend = library.async_http_transport.handle_async_request
    sync_inner: SyncSend = library.http_transport.handle_request

    async def guarded_async(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await sandbox.send_async(request, async_inner, transport, library=library)

    def guarded_sync(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return sandbox.send_sync(request, sync_inner, transport, library=library)

    setattr(library.async_http_transport, "handle_async_request", guarded_async)  # noqa: B010
    setattr(library.http_transport, "handle_request", guarded_sync)  # noqa: B010


def install(sandbox: Sandbox) -> None:
    """Close the process paths, add the audit hook, then patch every HTTP library's transports, for the rest of this
    process.

    In that order because finding the libraries imports httpx2 when the interpreter has it: a dependency outside the
    SDK, whose import-time code runs under the hook like everything after it, not before it."""
    _processes(sandbox)
    _audit(sandbox)
    for library in libraries():
        _transports(sandbox, library)
