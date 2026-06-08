# -*- coding: utf-8 -*-
"""
Tests for ExifToolServer singleton lock (first-come, first-served).
"""

import json
import os
import socket
import sys
import tempfile
import time
import unittest

import exiftool
from exiftool.exceptions import ExifToolServerError


def _ping(host: str, port: int, timeout: float = 5.0) -> bool:
	"""Check if a server is alive."""
	try:
		s = socket.create_connection((host, port), timeout=timeout)
		req = json.dumps({"id": 1, "method": "ping", "params": {}}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		return resp is not None and '"pong"' in resp
	except (OSError, socket.timeout, ConnectionError):
		return False


def _shutdown(host: str, port: int, timeout: float = 5.0):
	"""Shut down a server via RPC."""
	try:
		s = socket.create_connection((host, port), timeout=timeout)
		req = json.dumps({"id": 1, "method": "shutdown", "params": {}}) + "\n"
		s.sendall(req.encode())
		s.close()
	except (OSError, socket.timeout, ConnectionError):
		pass


def _wait_for_port_closed(host: str, port: int, timeout: float = 5.0) -> bool:
	"""Wait until a port is no longer listening."""
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		try:
			with socket.create_connection((host, port), timeout=0.5):
				pass
		except (OSError, socket.timeout, ConnectionError):
			return True
		time.sleep(0.05)
	return False


@unittest.skipIf(
	os.environ.get("SKIP_SINGLETON_TESTS"),
	"Skipping singleton tests that use subprocess",
)
class TestExifToolServerSingleton(unittest.TestCase):
	"""Test singleton lock enforcement on ExifToolServer."""

	@classmethod
	def setUpClass(cls):
		cls.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-singleton.json")
		cls.lock_file = cls.port_file + ".lock"
		for p in (cls.port_file, cls.lock_file):
			try:
				os.unlink(p)
			except OSError:
				pass

	@classmethod
	def tearDownClass(cls):
		for p in (cls.port_file, cls.lock_file):
			try:
				os.unlink(p)
			except OSError:
				pass

	def setUp(self):
		self.server = exiftool.ExifToolServer(
			port_file=self.port_file,
			singleton=True,
			no_exiftool=True,
		)

	def tearDown(self):
		self.server.stop()

	def test_singleton_lock_acquired(self):
		"""Start with singleton=True should create and hold the lock file."""
		port = self.server.start()
		self.assertGreater(port, 0)
		self.assertTrue(os.path.exists(self.lock_file))

	def test_singleton_idempotent(self):
		"""Starting an already-running singleton server is idempotent."""
		p1 = self.server.start()
		p2 = self.server.start()
		self.assertEqual(p1, p2)

	def test_singleton_second_fails(self):
		"""A second server must raise when the lock is held by a live server."""
		self.server.start()
		server2 = exiftool.ExifToolServer(
			port_file=self.port_file,
			singleton=True,
			no_exiftool=True,
		)
		with self.assertRaises(ExifToolServerError) as cm:
			server2.start()
		self.assertIn("already running", str(cm.exception).lower())

	def test_singleton_lock_cleaned_up(self):
		"""After stop() the lock file must be released (re-acquirable)."""
		self.server.start()
		self.server.stop()
		server2 = exiftool.ExifToolServer(
			port_file=self.port_file,
			singleton=True,
			no_exiftool=True,
		)
		try:
			port2 = server2.start()
			self.assertGreater(port2, 0)
		finally:
			server2.stop()

	def test_stale_lock_takeover(self):
		"""A crashed/exited server leaves a stale lock that a new server takes over."""
		self.server.start()
		self.server.stop()
		# Lock file may still exist but flock is released
		new_server = exiftool.ExifToolServer(
			port_file=self.port_file,
			singleton=True,
			no_exiftool=True,
		)
		try:
			port = new_server.start()
			self.assertGreater(port, 0)
			# Verify it's the new server responding
			s = socket.create_connection(("127.0.0.1", port), timeout=5)
			req = json.dumps({"id": 1, "method": "ping", "params": {}}) + "\n"
			s.sendall(req.encode())
			resp = s.makefile("r", encoding="utf-8").readline()
			s.close()
			self.assertIn("pong", resp)
		finally:
			new_server.stop()

	def test_singleton_stress_concurrent(self):
		"""N concurrent processes with singleton=True: exactly one succeeds."""
		N = 10
		root = os.path.join(os.path.dirname(__file__), "..")

		import subprocess
		procs = []
		for i in range(N):
			delay = (N - 1 - i) * 0.01
			sub_code = (
				"import time; time.sleep(%s)\n"
				"import sys, json, os\n"
				"sys.path.insert(0, %r)\n"
				"import exiftool\n"
				"port_file = %r\n"
				"result = {'pid': os.getpid(), 'status': 'unknown'}\n"
				"try:\n"
				"    srv = exiftool.ExifToolServer(\n"
				"        port_file=port_file, singleton=True,\n"
				"        no_exiftool=True, idle_timeout=30)\n"
				"    port = srv.start()\n"
				"    result['status'] = 'running'\n"
				"    result['port'] = port\n"
				"except Exception as e:\n"
				"    result['status'] = 'failed'\n"
				"    result['error'] = type(e).__name__\n"
				"# Print immediately — always before the while loop\n"
				"sys.stdout.write(json.dumps(result) + '\\n')\n"
				"sys.stdout.flush()\n"
				"if result['status'] == 'running':\n"
				"    while srv.running:\n"
				"        time.sleep(0.2)\n"
				"    result['status'] = 'stopped'\n"
				"    # Print final status\n"
				"    sys.stdout.write(json.dumps(result) + '\\n')\n"
				"    sys.stdout.flush()\n"
			) % (delay, root, self.port_file)
			p = subprocess.Popen(
				[sys.executable, "-c", sub_code],
				stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			)
			procs.append(p)

		deadline = time.monotonic() + 15.0
		read_procs = list(procs)

		# Read one line from each process (the initial status)
		results = []
		for p in read_procs:
			try:
				line = p.stdout.readline()
				if line:
					results.append(json.loads(line.decode()))
			except (OSError, socket.timeout, EOFError,
			        json.JSONDecodeError):
				pass

		successes = [r for r in results if r['status'] == 'running']
		failures = [r for r in results if r['status'] == 'failed']
		self.assertEqual(len(successes), 1,
			f"Expected exactly 1 success, got {len(successes)}")
		self.assertEqual(len(failures), N - 1,
			f"Expected {N-1} failures, got {len(failures)}")

		# Verify the survivor is still serving
		port = successes[0]['port']
		self.assertTrue(_ping("127.0.0.1", port, timeout=5.0),
			"Survivor should be reachable")

		# Shutdown the survivor
		_shutdown("127.0.0.1", port)
		_wait_for_port_closed("127.0.0.1", port)

		# Cleanup survivors
		for p in procs:
			try:
				p.communicate(timeout=3.0)
			except subprocess.TimeoutExpired:
				p.kill()


if __name__ == '__main__':
	unittest.main()
