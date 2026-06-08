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

This submodule contains the :py:class:`ExifToolServer` class, which wraps an
:py:class:`exiftool.ExifTool` instance and exposes its API via a TCP socket
using a JSON-RPC-like protocol.

This enables multiple processes or threads to share a single ``exiftool``
subprocess, avoiding the overhead of starting a new subprocess per client.

Usage::

    import exiftool

    # Start a server on a random port
    server = exiftool.ExifToolServer()
    port = server.start()
    print(f"Server listening on port {port}")

    # Keep running until idle timeout or explicit stop
    # server.stop()

"""

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from .constants import (
	DEFAULT_SERVER_HOST,
	DEFAULT_SERVER_PORT,
	DEFAULT_SERVER_PORT_FILE,
	DEFAULT_SERVER_PORT_FILE_DIR,
	DEFAULT_SERVER_TIMEOUT,
	DEFAULT_SERVER_IDLE_TIMEOUT,
)
from .exceptions import (
	ExifToolServerError,
	ExifToolExecuteError,
	ExifToolOutputEmptyError,
	ExifToolJSONInvalidError,
)
from .helper import ExifToolHelper


if os.name == 'nt':
	import msvcrt as _msvcrt
	_PLATFORM = 'windows'
else:
	import fcntl as _fcntl
	_PLATFORM = 'posix'


class _FileLock:
	"""Cross-platform exclusive file lock.

	Uses ``fcntl.flock`` on Unix and ``msvcrt.locking`` on Windows.
	The lock file is never deleted — it acts as a persistent mutex.
	"""

	def __init__(self, path: str):
		self._path = path
		self._fd: int | None = None

	def acquire(self, blocking: bool = True) -> bool:
		"""Try to acquire the exclusive lock.

		Returns True if the lock was acquired.  When *blocking* is False
		and the lock is held by another process, returns False immediately.
		"""
		self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
		try:
			if _PLATFORM == 'windows':
				flags = _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK
				_msvcrt.locking(self._fd, flags, 1)
				return True
			else:
				flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
				_fcntl.flock(self._fd, flags)
				return True
		except (BlockingIOError, OSError):
			os.close(self._fd)
			self._fd = None
			return False

	def release(self):
		"""Release the lock."""
		if self._fd is not None:
			try:
				os.close(self._fd)
			except OSError:
				pass
			self._fd = None


_PROTOCOL_VERSION = 1


def _iso_now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _log(msg: str):
	print(f"[{_iso_now()} exiftool-server] {msg}", file=sys.stderr, flush=True)


def _read_port_file(port_file: str) -> dict | None:
	try:
		with open(port_file) as f:
			return json.load(f)
	except (OSError, json.JSONDecodeError):
		return None


def _write_port_file(port_file: str, data: dict):
	tmp = port_file + ".tmp"
	with open(tmp, "w") as f:
		json.dump(data, f)
		f.flush()
		os.fsync(f.fileno())
	os.replace(tmp, port_file)


def _remove_port_file(port_file: str, pid: int):
	data = _read_port_file(port_file)
	if data and data.get("pid") == pid:
		try:
			os.unlink(port_file)
		except OSError:
			pass


def _ping_server(host: str, port: int, timeout: float = 5.0) -> bool:
	"""Ping a server and return True if it responds."""
	try:
		s = socket.create_connection((host, port), timeout=timeout)
		req = json.dumps({"id": 1, "method": "ping", "params": {}}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		return resp is not None and '"pong"' in resp
	except (OSError, socket.timeout, ConnectionError):
		return False


def _send_shutdown(host: str, port: int, timeout: float = 5.0) -> bool:
	"""Send shutdown command to a server."""
	try:
		s = socket.create_connection((host, port), timeout=timeout)
		req = json.dumps({"id": 1, "method": "shutdown", "params": {}}) + "\n"
		s.sendall(req.encode())
		s.close()
		return True
	except (OSError, socket.timeout, ConnectionError):
		return False


def _lookup_port_file(port_file: str | None = None) -> dict | None:
	"""Read the port file and return its contents, or None."""
	if port_file is None:
		port_file = os.path.join(DEFAULT_SERVER_PORT_FILE_DIR, DEFAULT_SERVER_PORT_FILE)
	return _read_port_file(port_file)


def _lock_path(port_file: str | None = None) -> str:
	"""Return the lock file path derived from the port file path."""
	if port_file is None:
		port_file = os.path.join(DEFAULT_SERVER_PORT_FILE_DIR, DEFAULT_SERVER_PORT_FILE)
	return port_file + ".lock"


def find_server(port_file: str | None = None, timeout: float = 5.0) -> int | None:
	"""Find a running ExifTool server by reading the port file.

	Returns the port number if the server is reachable, or ``None``.
	"""
	data = _lookup_port_file(port_file)
	if data is None:
		return None
	port = data.get("port")
	if port is None:
		return None
	if _ping_server("127.0.0.1", port, timeout=timeout):
		return port
	return None


def _build_server_args(port_file: str, executable: str | None,
                       common_args: list[str] | None,
                       singleton: bool) -> list[str]:
	"""Build the argument list for the server subprocess."""
	args = [sys.executable, "-m", "exiftool.server", "--port-file", port_file]
	if executable:
		args.extend(["--executable", executable])
	if common_args:
		for ca in common_args:
			args.extend(["--common-arg", ca])
	if singleton:
		args.append("--singleton")
	return args


def _wait_for_port(port_file: str, timeout: float) -> int:
	"""Poll the port file until the server is reachable, return port."""
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		try:
			data = _read_port_file(port_file)
			if data and "port" in data:
				port = data["port"]
				if _ping_server("127.0.0.1", port, timeout=1.0):
					return port
		except (OSError, socket.timeout, ConnectionError):
			pass
		time.sleep(0.05)
	raise TimeoutError


def spawn_server(timeout: float = DEFAULT_SERVER_TIMEOUT,
                 port_file: str | None = None,
                 executable: str | None = None,
                 common_args: list[str] | None = None,
                 singleton: bool = False) -> int:
	"""Spawn an ExifTool server as a background subprocess.

	When *singleton* is True, first checks for an existing server and
	returns its port if it is reachable, avoiding a duplicate spawn.

	Returns the port the server is listening on.
	Raises :py:class:`exiftool.exceptions.ExifToolServerError` if the server
	does not start within *timeout* seconds.
	"""
	import subprocess

	if port_file is None:
		port_file = os.path.join(DEFAULT_SERVER_PORT_FILE_DIR, DEFAULT_SERVER_PORT_FILE)

	if singleton:
		existing = find_server(port_file, timeout=2.0)
		if existing is not None:
			return existing

	try:
		os.unlink(port_file)
	except OSError:
		pass

	args = _build_server_args(port_file, executable, common_args, singleton)

	proc = subprocess.Popen(
		args,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	proc._child_created = False

	try:
		return _wait_for_port(port_file, timeout)
	except TimeoutError:
		try:
			proc.terminate()
		except OSError:
			pass
		raise ExifToolServerError(
			f"ExifTool server did not start within {timeout}s")


class ExifToolServer:
	"""TCP server that wraps an :py:class:`exiftool.ExifToolHelper` instance.

	Listens on a TCP socket and accepts JSON-RPC-like requests, delegating
	each to the underlying exiftool subprocess.  The server is single-threaded;
	each request is fully handled before the next is accepted, providing
	natural serialisation.

	Args:
		host: Interface to bind to (default 127.0.0.1).
		port: Port to bind to (0 = random available port).
		idle_timeout: Seconds of inactivity before auto-shutdown (0 = no timeout).
		executable: Path to the exiftool executable.
		common_args: Additional arguments passed to every exiftool command.
		port_file: Path to port discovery file (None = use default).
		singleton: If True, enforce a single server per-lock-file via a
			cross-platform exclusive file lock (first-come, first-served).
			When a new server starts and the lock is held by an alive
			server, :py:class:`ExifToolServerError` is raised.  If the
			previous server has exited, the lock is taken over.
		no_exiftool: If True, skip starting the exiftool subprocess (testing only).
	"""

	def __init__(self, host: str = DEFAULT_SERVER_HOST,
	             port: int = DEFAULT_SERVER_PORT,
	             idle_timeout: float = DEFAULT_SERVER_IDLE_TIMEOUT,
	             executable: str | None = None,
	             common_args: list[str] | None = None,
	             port_file: str | None = None,
	             singleton: bool = False,
	             no_exiftool: bool = False):

		self._host = host
		self._port = port
		self._idle_timeout = idle_timeout
		self._port_file = port_file
		self._singleton = singleton
		self._no_exiftool = no_exiftool

		self._helper: ExifToolHelper | None = None
		self._server: socket.socket | None = None
		self._active = False
		self._actual_port = 0
		self._last_request_time = 0.0
		self._lock = threading.Lock()
		self._file_lock: _FileLock | None = None

		if no_exiftool:
			executable = None
		self._executable = executable
		self._common_args = common_args or []

	@property
	def port(self) -> int:
		"""The TCP port the server is listening on."""
		return self._actual_port

	@property
	def running(self) -> bool:
		"""Whether the server is currently running."""
		return self._active

	def _acquire_singleton_lock(self):
		"""Try to acquire the singleton lock (first-come, first-served).

		Attempts a non-blocking exclusive file lock.  If the lock is already
		held by another server, reads the port file and raises an error with
		the address of the running server.

		Raises :py:class:`ExifToolServerError` if another server is running.
		"""
		lock_path = _lock_path(self._port_file)
		flock = _FileLock(lock_path)

		if flock.acquire(blocking=False):
			self._file_lock = flock
			return

		data = _lookup_port_file(self._port_file)
		if data is not None:
			other_port = data.get("port")
			other_pid = data.get("pid")
			if other_port and other_pid is not None:
				alive = _ping_server("127.0.0.1", other_port, timeout=2.0)
				if alive:
					raise ExifToolServerError(
						f"Server already running on port {other_port} "
						f"(PID {other_pid}). Set singleton=False or stop "
						f"the existing server first.")
				# Stale lock — take it over
				flock.acquire(blocking=True)
				self._file_lock = flock
				return

		raise ExifToolServerError(
			f"Could not acquire singleton lock: {lock_path}")

	def start(self) -> int:
		"""Start the server.

		This method starts the exiftool subprocess, binds the TCP socket,
		and begins accepting connections in a background thread.  It blocks
		until the socket is ready.

		If *singleton* was set to True in the constructor, this method
		first attempts to acquire a cross-platform file lock (first-come,
		first-served).  See the class docstring for details.

		Returns the port number the server is listening on.
		"""
		with self._lock:
			if self._active:
				return self._actual_port

			if self._singleton:
				self._acquire_singleton_lock()

			# Start exiftool subprocess
			common_args = list(self._common_args)
			if not self._no_exiftool:
				self._helper = ExifToolHelper(
					common_args=common_args if common_args else None,
					check_execute=False,
				)
				self._helper.run()

			# Bind TCP socket
			self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			self._server.bind((self._host, self._port))
			self._server.listen(10)
			self._server.settimeout(1.0)
			self._actual_port = self._server.getsockname()[1]

			self._active = True
			self._last_request_time = time.monotonic()

			# Write port file for client discovery
			self._write_port_file()

			# Start accept loop in background thread
			t = threading.Thread(target=self._accept_loop, daemon=True)
			t.start()

			# Start watchdog if idle_timeout > 0
			if self._idle_timeout > 0:
				wt = threading.Thread(target=self._watchdog, daemon=True)
				wt.start()

			return self._actual_port

	def stop(self):
		"""Stop the server gracefully."""
		with self._lock:
			if not self._active:
				return
			self._active = False
			if self._server:
				try:
					self._server.close()
				except OSError:
					pass
			if self._helper:
				try:
					self._helper.terminate()
				except Exception:
					pass
			self._remove_port_file()
			if self._file_lock:
				self._file_lock.release()
				self._file_lock = None

	def _write_port_file(self):
		if self._port_file is None:
			return
		data = {
			"port": self._actual_port,
			"pid": os.getpid(),
			"protocol_version": _PROTOCOL_VERSION,
			"started_at": _iso_now(),
		}
		_write_port_file(self._port_file, data)

	def _remove_port_file(self):
		if self._port_file is None:
			return
		_remove_port_file(self._port_file, os.getpid())

	def _accept_loop(self):
		"""Accept connections in a loop (runs in background thread)."""
		while self._active:
			try:
				conn, addr = self._server.accept()
			except socket.timeout:
				continue
			except OSError:
				break
			self._handle_connection(conn)
		self._remove_port_file()

	def _handle_connection(self, conn: socket.socket):
		"""Handle a single client connection synchronously."""
		with conn:
			conn.settimeout(30.0)
			try:
				reader = conn.makefile("r", encoding="utf-8")
				line = reader.readline()
				if not line:
					return
				response = self._dispatch(line.strip())
				conn.sendall((response + "\n").encode())
			except (OSError, socket.timeout):
				pass
			finally:
				self._last_request_time = time.monotonic()

	def _dispatch(self, request: str) -> str:
		"""Parse a JSON-RPC request and return a JSON response."""
		try:
			req = json.loads(request)
		except json.JSONDecodeError as e:
			return json.dumps({
				"id": None,
				"error": {"code": -32700, "message": f"Parse error: {e}"},
			})

		req_id = req.get("id")
		method = req.get("method", "")
		params = req.get("params", {})

		handler = getattr(self, f"_rpc_{method}", None)
		if handler is None:
			return json.dumps({
				"id": req_id,
				"error": {"code": -32601, "message": f"Unknown method: {method}"},
			})

		try:
			result = handler(**params)
			return json.dumps({"id": req_id, "result": result})
		except Exception as e:
			return json.dumps({
				"id": req_id,
				"error": {
					"code": -32603,
					"message": f"{type(e).__name__}: {e}",
				},
			})

	# ── RPC methods ──────────────────────────────────────────────────

	def _rpc_ping(self) -> str:
		"""Health check."""
		return "pong"

	def _rpc_status(self) -> dict:
		"""Return server status information."""
		elapsed = time.monotonic() - self._last_request_time
		return {
			"port": self._actual_port,
			"pid": os.getpid(),
			"idle_seconds": round(elapsed, 1),
			"idle_timeout": self._idle_timeout,
			"protocol_version": _PROTOCOL_VERSION,
		}

	def _rpc_shutdown(self) -> str:
		"""Shut down the server."""
		def _delayed_stop():
			time.sleep(0.1)
			self.stop()
		threading.Thread(target=_delayed_stop, daemon=True).start()
		return "shutting_down"

	def _rpc_execute(self, args: list[str]) -> dict:
		"""Execute an exiftool command and return the result.

		Args:
			args: List of command-line arguments to pass to exiftool.

		Returns:
			Dict with keys: stdout, stderr, status.
		"""
		if self._helper is None:
			raise ExifToolServerError("ExifTool not started (no_exiftool mode)")
		try:
			self._helper.execute(*args)
		except (ExifToolExecuteError, ExifToolOutputEmptyError,
		        ExifToolJSONInvalidError) as e:
			return {
				"stdout": e.stdout,
				"stderr": e.stderr,
				"status": e.returncode,
			}
		return {
			"stdout": self._helper.last_stdout,
			"stderr": self._helper.last_stderr,
			"status": self._helper.last_status,
		}

	def _rpc_execute_json(self, args: list[str]) -> list:
		"""Execute an exiftool command and return parsed JSON.

		Args:
			args: List of command-line arguments to pass to exiftool.

		Returns:
			Parsed JSON list of dicts.
		"""
		if self._helper is None:
			raise ExifToolServerError("ExifTool not started (no_exiftool mode)")
		return self._helper.execute_json(*args)

	def _rpc_get_metadata(self, files: list[str],
	                       params: list[str] | None = None) -> list:
		"""Get all metadata for the given files."""
		if self._helper is None:
			raise ExifToolServerError("ExifTool not started (no_exiftool mode)")
		return self._helper.get_metadata(files, params=params)

	def _rpc_get_tags(self, files: list[str], tags: list[str],
	                   params: list[str] | None = None) -> list:
		"""Get specific tags for the given files."""
		if self._helper is None:
			raise ExifToolServerError("ExifTool not started (no_exiftool mode)")
		return self._helper.get_tags(files, tags, params=params)

	def _rpc_set_tags(self, files: list[str], tags: dict,
	                   params: list[str] | None = None) -> str:
		"""Set specific tags on the given files."""
		if self._helper is None:
			raise ExifToolServerError("ExifTool not started (no_exiftool mode)")
		return self._helper.set_tags(files, tags, params=params)

	def _rpc_available(self) -> bool:
		"""Check if the exiftool subprocess is available."""
		if self._helper is None:
			return False
		try:
			self._helper.execute("-ver")
			return True
		except Exception:
			return False

	def _watchdog(self):
		"""Auto-shutdown after idle_timeout seconds of inactivity."""
		interval = max(1.0, self._idle_timeout / 4)
		while self._active:
			time.sleep(interval)
			if not self._active:
				break
			elapsed = time.monotonic() - self._last_request_time
			if elapsed > self._idle_timeout:
				_log(f"Idle timeout ({elapsed:.0f}s > {self._idle_timeout}s), shutting down")
				self.stop()


def main():
	"""CLI entry point for the server process."""
	import argparse

	port_file = os.path.join(DEFAULT_SERVER_PORT_FILE_DIR, DEFAULT_SERVER_PORT_FILE)

	parser = argparse.ArgumentParser(description="ExifTool server daemon")
	parser.add_argument(
		"--host", default=DEFAULT_SERVER_HOST,
		help=f"Interface to bind to (default: {DEFAULT_SERVER_HOST})")
	parser.add_argument(
		"--port", type=int, default=DEFAULT_SERVER_PORT,
		help=f"Port to bind to (default: {DEFAULT_SERVER_PORT}, 0=random)")
	parser.add_argument(
		"--idle-timeout", type=float, default=DEFAULT_SERVER_IDLE_TIMEOUT,
		help=f"Idle timeout in seconds (default: {DEFAULT_SERVER_IDLE_TIMEOUT})")
	parser.add_argument(
		"--port-file", default=port_file,
		help=f"Port discovery file (default: {port_file})")
	parser.add_argument(
		"--executable",
		help="Path to exiftool executable")
	parser.add_argument(
		"--common-arg", action="append", default=[],
		help="Common argument passed to every exiftool command")
	parser.add_argument(
		"--no-exiftool", action="store_true",
		help="Skip starting exiftool (testing only)")
	parser.add_argument(
		"--singleton", action="store_true",
		help="Enforce a single server per lock file")
	parser.add_argument(
		"--log", action="store_true",
		help="Enable server logging to stderr")

	args = parser.parse_args()
	if not args.log:
		global _log
		def _noop(msg): pass
		_log = _noop

	server = ExifToolServer(
		host=args.host,
		port=args.port,
		idle_timeout=args.idle_timeout,
		executable=args.executable,
		common_args=args.common_arg if args.common_arg else None,
		port_file=args.port_file,
		singleton=args.singleton,
		no_exiftool=args.no_exiftool,
	)

	try:
		port = server.start()
		if args.log:
			print(
				f"Server started on {args.host}:{port} (pid={os.getpid()})",
				file=sys.stderr)
		while server.running:
			time.sleep(1)
	except KeyboardInterrupt:
		server.stop()


if __name__ == "__main__":
	main()
