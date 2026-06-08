Server / Client Mode
====================

PyExifTool provides a TCP server mode that allows multiple processes or threads
to share a single ``exiftool`` subprocess.  This avoids the overhead of starting
a new subprocess per client and is essential when concurrent writers must not
write to the same filesystem at the same time, because the server serialises
all requests.

.. contents:: :local:

Architecture
------------

The server is built from three components:

``ExifToolServer``
    Wraps an :py:class:`exiftool.ExifToolHelper` instance and exposes its API
    over a TCP socket using a JSON-RPC-like protocol.  The server is
    **single-threaded** — it processes one request at a time.

``ExifToolClient``
    Connects to a running server and proxies all calls to it.  Provides the
    same method signatures as :py:class:`exiftool.ExifToolHelper`, making it a
    drop-in replacement.

``spawn_server()``
    Convenience function that launches a server as a background subprocess.
    Returns the TCP port it is listening on.

Quick-start::

    import exiftool

    # --- Process A: start a server ---
    server = exiftool.ExifToolServer()
    port = server.start()

    # --- Process B (or the same process): connect ---
    with exiftool.ExifToolClient(port=port) as client:
        metadata = client.get_metadata("file.jpg")

    # Servers auto-shutdown after the idle timeout; stop explicitly:
    server.stop()

Singleton Lock (First-Come, First-Served)
-----------------------------------------

When ``singleton=True`` is passed to the constructor, the server enforces that
only one instance runs at a time for a given *port file*.  The mechanism is a
cross-platform exclusive file lock (``fcntl.flock`` on Unix, ``msvcrt.locking``
on Windows).

**Lock acquisition rules:**

1. **First server to acquire the lock wins.**  It starts, writes the port file,
   and begins accepting requests.

2. **Second server fails immediately.**  If the lock is already held by a
   server that responds to a TCP ping, the second server raises
   :py:class:`exiftool.exceptions.ExifToolServerError` with the running
   server's port and PID.  No PID comparison, no retry loop, no takeover.

3. **Stale lock takeover.**  If the previous server has exited (or crashed),
   the OS releases the file lock automatically (``flock`` semantics).  A new
   server acquires it on the first attempt and starts normally.

This design guarantees that **no server ever interrupts another server's
requests**.  There is no PID election, no kill signals, and no takeovers
during operation.

Request Handling
----------------

Connection model
    Each TCP connection carries exactly **one** request.  The server reads a
    single line, dispatches it, sends a single JSON response line, and closes
    the connection.  There is no HTTP keep-alive or pipelining.

Concurrency
    The server is **single-threaded**.  All requests are serialised by the
    accept loop.  While one request is being processed (e.g. a long-running
    ``get_metadata``), subsequent connections wait in the kernel's TCP
    backlog (default size 10).

Protocol
    Requests are JSON-RPC-like objects terminated by a newline::

        {"id": 1, "method": "ping", "params": {}}

    Responses follow the same format with a ``result`` or ``error`` key::

        {"id": 1, "result": "pong"}

    Error codes follow JSON-RPC 2.0 conventions:

    ======== ===================================
    Code     Meaning
    ======== ===================================
    -32700   Parse error (invalid JSON)
    -32601   Unknown method
    -32603   Internal error (exception in handler)
    ======== ===================================

Supported RPC methods

    ``ping``
        Returns ``"pong"``.  Health check that does not touch the exiftool
        subprocess.

    ``status``
        Returns a dict with ``port``, ``pid``, ``idle_seconds``,
        ``idle_timeout``, and ``protocol_version``.  Does not touch the
        exiftool subprocess.

    ``shutdown``
        Triggers a delayed server shutdown (0.1 s delay so the response is
        sent before ``stop()`` runs).

    ``execute``
        Calls :py:meth:`exiftool.ExifToolHelper.execute` and returns
        ``{stdout, stderr, status}``.

    ``execute_json``
        Calls :py:meth:`exiftool.ExifToolHelper.execute_json` and returns
        the parsed JSON list.

    ``get_metadata``
        Calls :py:meth:`exiftool.ExifToolHelper.get_metadata`.

    ``get_tags``
        Calls :py:meth:`exiftool.ExifToolHelper.get_tags`.

    ``set_tags``
        Calls :py:meth:`exiftool.ExifToolHelper.set_tags`.

    ``available``
        Returns ``True`` if the exiftool subprocess is running and responds
        to ``exiftool -ver``.

In-Flight Request During Shutdown
---------------------------------

When a server receives a ``shutdown`` RPC call:

1. The shutdown handler spawns a daemon thread that sleeps 0.1 s and then
   calls ``stop()``.
2. The original ``_handle_connection`` that received the shutdown request
   **completes normally** — the client gets its response.
3. ``stop()`` closes the **listening** socket (no new connections accepted),
   terminates the exiftool subprocess, removes the port file, and releases
   the singleton lock.
4. Any **other** in-flight ``_handle_connection`` threads (if multiple
   connections were accepted before the listening socket was closed) also
   complete before the process exits, because they are daemon threads and
   run to completion.

If the exiftool subprocess is killed externally (e.g. a concurrent shutdown,
a system administrator killing the process, or a crash) while a request is
being dispatched:

- The dispatch error handler in ``_dispatch`` catches the resulting
  ``OSError`` / ``BrokenPipeError`` and returns a JSON-RPC error response
  to the client.
- The client **does not hang** — it receives a clean error response.
- The server process itself continues running (only the exiftool subprocess
  died).  Subsequent RPC calls will fail until the server is restarted.

Idle Timeout
------------

The server can auto-shutdown after a period of inactivity::

    server = ExifToolServer(idle_timeout=60.0)  # default

- The idle timer resets after **every** completed request.
- A watchdog thread checks every ``max(1.0, idle_timeout / 4)`` seconds.
- If no request has been received for longer than ``idle_timeout`` seconds,
  the watchdog calls ``stop()``.
- Setting ``idle_timeout=0`` disables auto-shutdown.

Client Behaviour
----------------

:py:class:`exiftool.ExifToolClient` does **not** manage the exiftool subprocess
itself — all commands are forwarded to the server via TCP.

``run()`` and ``terminate()``
    No-ops for the client.  The server manages the subprocess lifecycle.

Context manager
    The client can be used as a context manager.  ``__exit__`` is a no-op
    (it does not shut down the server, because other clients may be using it).

Auto-discovery
    If no ``port`` is given, the client reads the port file
    (``~/.cache/pyexiftool/pyexiftool-server.json`` by default), pings the
    server to verify it is alive, and connects.  Raises
    :py:class:`exiftool.exceptions.ExifToolConnectionError` if no server is
    found.

``spawn_server()`` Utility
--------------------------

:py:func:`exiftool.spawn_server` launches a server as a background
subprocess.  When called with ``singleton=True``, it first calls
``find_server()``.  If a server is already running and reachable, it returns
the existing port **without** spawning a new process::

    # Reuse an existing server, or create one if none exists
    port = exiftool.spawn_server(singleton=True)

    # Always start a new server (may coexist with others)
    port = exiftool.spawn_server()

This is the only "reuse" path — ``ExifToolServer(singleton=True).start()``
always raises if the lock is held.

Non-Singleton Mode
------------------

When ``singleton=False`` (the default), multiple servers can coexist on
different ports.  Each manages its own exiftool subprocess.  This is useful
for:

- Isolating workloads between different parts of an application.
- Running test servers in parallel without interfering.
- Load-balancing across multiple exiftool processes.

Port File Discovery
-------------------

The server writes a JSON port file to ``~/.cache/pyexiftool/pyexiftool-server.json``
(default) when it starts.  The file contains::

    {
        "port": 43291,
        "pid": 12345,
        "protocol_version": 1,
        "started_at": "2026-06-08T12:00:00+00:00"
    }

Clients read this file to auto-discover the server.  The directory is
chosen to be user-specific (via ``~/.cache``) so that all processes for the
same user share the same discovery file regardless of ``tempfile.gettempdir()``
differences.

Both the path and the directory can be overridden via the ``port_file``
parameter or the ``PYEXIFTOOL_PORT_FILE`` environment variable.

Error Handling Summary
----------------------

+-----------------------------------------+------------------------------------------+
| Scenario                                | Client experience                        |
+=========================================+==========================================+
| No server running                       | ``ExifToolConnectionError``              |
+-----------------------------------------+------------------------------------------+
| Server starts with singleton lock held  | ``ExifToolServerError`` (includes port)  |
+-----------------------------------------+------------------------------------------+
| Exiftool subprocess dies mid-request    | JSON-RPC error response (does not hang)  |
+-----------------------------------------+------------------------------------------+
| Server shuts down during request        | Request completes; response still sent   |
+-----------------------------------------+------------------------------------------+
| Invalid JSON sent to server             | JSON-RPC error ``-32700``                |
+-----------------------------------------+------------------------------------------+
| Unknown RPC method                      | JSON-RPC error ``-32601``                |
+-----------------------------------------+------------------------------------------+
| Server idle too long                    | Server shuts down; client gets           |
|                                         | ``ExifToolConnectionError`` on next call |
+-----------------------------------------+------------------------------------------+
