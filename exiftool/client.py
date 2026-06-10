# -*- coding: utf-8 -*-
#
# This file is part of PyExifTool.
#
# PyExifTool <http://github.com/sylikc/pyexiftool>
#
# Copyright 2019-2023 Kevin M (sylikc)
# Copyright 2012-2014 Sven Marnach
#
# Community contributors are listed in the CHANGELOG.md for the PRs
#
# PyExifTool is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the licence, or
# (at your option) any later version, or the BSD licence.
#
# PyExifTool is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See COPYING.GPL or COPYING.BSD for more details.


"""

This submodule contains the :py:class:`ExifToolClient` class, which connects
to a running :py:class:`exiftool.server.ExifToolServer` and provides the same
API as :py:class:`exiftool.ExifToolHelper`.

Usage::

    import exiftool

    # Connect to an existing server on port 12345
    with exiftool.ExifToolClient(port=12345) as client:
        metadata = client.get_metadata("file.jpg")

    # Auto-discover via port file
    with exiftool.ExifToolClient() as client:
        version = client.execute("-ver")

"""

import json
import os
import socket
from typing import Any, Optional, Union

from .constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT_FILE,
    DEFAULT_SERVER_PORT_FILE_DIR,
    DEFAULT_SERVER_TIMEOUT,
)
from .exceptions import (
    ExifToolConnectionError,
    ExifToolServerError,
)


# basestring compatibility (Python 3)
TUPLE_STR_BYTES = (bytes, str)


def _send_request(host: str, port: int, method: str,
                  params: dict | None = None,
                  timeout: float = DEFAULT_SERVER_TIMEOUT) -> Any:
    """Send a JSON-RPC request to the server and return the result."""
    if params is None:
        params = {}
    req = json.dumps({"id": 1, "method": method, "params": params})
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except (OSError, socket.timeout) as e:
        raise ExifToolConnectionError(
            f"Cannot connect to ExifTool server at {host}:{port}: {e}")

    try:
        s.settimeout(timeout)
        s.sendall((req + "\n").encode())
        resp_line = s.makefile("r", encoding="utf-8").readline()
        if not resp_line:
            raise ExifToolConnectionError(
                "Empty response from ExifTool server")
        data = json.loads(resp_line.strip())
        if "error" in data:
            err = data["error"]
            raise ExifToolServerError(
                f"Server error ({err.get('code', -1)}): "
                f"{err.get('message', 'unknown')}")
        return data.get("result")
    finally:
        s.close()


def _find_server(port_file: str | None = None,
                 timeout: float = DEFAULT_SERVER_TIMEOUT) -> tuple[str, int]:
    """Read the port file and return (host, port) of a running server.

    Raises ``ExifToolConnectionError`` if no server is found.
    """
    if port_file is None:
        port_file = os.path.join(DEFAULT_SERVER_PORT_FILE_DIR, DEFAULT_SERVER_PORT_FILE)
    try:
        with open(port_file) as f:
            data = json.load(f)
        port = int(data["port"])
        host = data.get("host", DEFAULT_SERVER_HOST)
        # Ping to verify it's alive
        _send_request(host, port, "ping", timeout=timeout)
        return host, port
    except (OSError, json.JSONDecodeError, KeyError,
            ExifToolConnectionError, ExifToolServerError) as exc:
        raise ExifToolConnectionError(
            f"Cannot reach ExifTool server: {exc}")


class ExifToolClient:
    """TCP client that connects to a running :py:class:`exiftool.server.ExifToolServer`.

    Provides the same method signatures as :py:class:`exiftool.ExifToolHelper`,
    making it a drop-in replacement.  The client does not manage an exiftool
    subprocess itself — all commands are forwarded to the server.

    If *port* is not specified, the client tries to auto-discover the server
    using the port discovery file (``pyexiftool-server.json`` in the temp
    directory).

    Args:
        host: Server hostname (default 127.0.0.1).
        port: Server TCP port, or None to auto-discover.
        port_file: Path to port discovery file (None = use default).
        timeout: Timeout for server requests in seconds.

    Raises:
        ExifToolConnectionError: If no server is found or connection fails.
    """

    def __init__(self, host: str = DEFAULT_SERVER_HOST,
                 port: int | None = None,
                 port_file: str | None = None,
                 timeout: float = DEFAULT_SERVER_TIMEOUT):
        self._timeout = timeout

        if port is not None:
            self._host = host
            self._port = port
            # Verify reachability
            try:
                _send_request(host, port, "ping", timeout=timeout)
            except ExifToolConnectionError:
                raise
            except Exception as e:
                raise ExifToolConnectionError(
                    f"ExifTool server at {host}:{port} is not reachable: {e}")
        else:
            self._host, self._port = _find_server(
                port_file=port_file, timeout=timeout)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass  # Don't shut down the server — other clients may be using it

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        """Send an RPC request and return the result."""
        return _send_request(
            self._host, self._port, method, params, timeout=self._timeout)

    # ── Process control (no-ops for client mode) ────────────────────

    @property
    def running(self) -> bool:
        """Whether the server is reachable."""
        try:
            self._rpc("ping")
            return True
        except Exception:
            return False

    @property
    def host(self) -> str:
        """The server host."""
        return self._host

    @property
    def port(self) -> int:
        """The server port."""
        return self._port

    def run(self):
        """No-op for client mode (server manages the subprocess)."""

    def terminate(self):
        """No-op for client mode (server manages the subprocess)."""

    # ── Execute methods ─────────────────────────────────────────────

    def execute(self, *params: Any, raw_bytes: bool = False) -> str:
        """Execute an exiftool command via the server.

        See :py:meth:`exiftool.ExifTool.execute` for parameter details.
        """
        # Convert non-str/bytes params to str (matching ExifToolHelper behavior)
        str_params = [
            x if isinstance(x, TUPLE_STR_BYTES) else str(x)
            for x in params
        ]
        result = self._rpc("execute", {"args": str_params})
        stdout = result.get("stdout", "")
        if isinstance(stdout, bytes) and not raw_bytes:
            return stdout.decode("utf-8")
        return stdout

    def execute_json(self, *params: Any) -> list:
        """Execute an exiftool command and return parsed JSON.

        See :py:meth:`exiftool.ExifTool.execute_json` for parameter details.
        """
        str_params = [
            x if isinstance(x, TUPLE_STR_BYTES) else str(x)
            for x in params
        ]
        return self._rpc("execute_json", {"args": str_params})

    # ── Helper methods ──────────────────────────────────────────────

    def get_metadata(self, files: Union[str, list],
                     params: Optional[Union[str, list]] = None) -> list:
        """Return all metadata for the given files.

        See :py:meth:`exiftool.ExifToolHelper.get_metadata` for details.
        """
        file_list = self._parse_files(files)
        params_list = self._parse_params(params)
        return self._rpc("get_metadata", {
            "files": file_list,
            "params": params_list,
        })

    def get_tags(self, files: Union[str, list],
                 tags: Optional[Union[str, list]] = None,
                 params: Optional[Union[str, list]] = None) -> list:
        """Return specified tags for the given files.

        See :py:meth:`exiftool.ExifToolHelper.get_tags` for details.
        """
        file_list = self._parse_files(files)
        tag_list = self._parse_params(tags)
        params_list = self._parse_params(params)
        return self._rpc("get_tags", {
            "files": file_list,
            "tags": tag_list,
            "params": params_list,
        })

    def set_tags(self, files: Union[str, list], tags: dict,
                 params: Optional[Union[str, list]] = None) -> str:
        """Set tag values on the given files.

        See :py:meth:`exiftool.ExifToolHelper.set_tags` for details.
        """
        file_list = self._parse_files(files)
        params_list = self._parse_params(params)
        return self._rpc("set_tags", {
            "files": file_list,
            "tags": tags,
            "params": params_list,
        })

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_files(files) -> list:
        """Normalise the *files* argument to a list of strings.

        Matches the behaviour of :py:meth:`exiftool.ExifToolHelper._parse_arg_files`.
        """
        if isinstance(files, TUPLE_STR_BYTES):
            return [str(files)]
        # Check if it's iterable (but not a string/bytes)
        try:
            iter(files)
        except TypeError:
            return [str(files)]
        return [str(f) for f in files]

    @staticmethod
    def _parse_params(params: Optional[Union[str, list]]) -> list | None:
        """Normalise optional params argument."""
        if params is None:
            return None
        if isinstance(params, TUPLE_STR_BYTES):
            return [str(params)]
        try:
            iter(params)
        except TypeError:
            return [str(params)]
        return [str(p) for p in params]
