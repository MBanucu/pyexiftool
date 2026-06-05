# -*- coding: utf-8 -*-
"""
Tests for ExifToolServer and ExifToolClient (TCP JSON-RPC protocol).
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest

import exiftool
from exiftool.exceptions import (
	ExifToolConnectionError,
	ExifToolServerError,
)
from exiftool.constants import PLATFORM_WINDOWS


# Path to a known test image
from tests.common_util import TEST_IMAGE_JPG


SERVER_START_TIMEOUT = 15.0


class TestExifToolServer(unittest.TestCase):
	"""Test the ExifToolServer class directly."""

	@classmethod
	def setUpClass(cls):
		cls.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-server.json")
		# Clean up any stale port file
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	def setUp(self):
		self.server = exiftool.ExifToolServer(
			port_file=self.port_file,
			no_exiftool=True,
		)
		self.port = self.server.start()

	def tearDown(self):
		self.server.stop()
		try:
			os.unlink(self.port_file)
		except OSError:
			pass

	def test_server_listens(self):
		"""Server should be listening on the assigned port."""
		self.assertGreater(self.port, 0)
		self.assertTrue(self.server.running)

	def test_server_ping(self):
		"""Ping should return pong."""
		s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
		req = json.dumps({"id": 1, "method": "ping", "params": {}}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		data = json.loads(resp.strip())
		self.assertEqual(data["result"], "pong")

	def test_server_unknown_method(self):
		"""Unknown method should return -32601 error."""
		s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
		req = json.dumps({"id": 1, "method": "nonexistent", "params": {}}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		data = json.loads(resp.strip())
		self.assertEqual(data["error"]["code"], -32601)

	def test_server_bad_json(self):
		"""Invalid JSON should return -32700 error."""
		s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
		s.sendall(b"not json\n")
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		data = json.loads(resp.strip())
		self.assertEqual(data["error"]["code"], -32700)

	def test_server_status(self):
		"""Status should return server metadata."""
		result = self._rpc("status")
		self.assertIn("port", result)
		self.assertIn("pid", result)
		self.assertEqual(result["port"], self.port)

	def test_server_available_no_exiftool(self):
		"""available() should return False when no_exiftool=True."""
		result = self._rpc("available")
		self.assertFalse(result)

	def test_server_shutdown(self):
		"""Shutdown should stop the server."""
		result = self._rpc("shutdown")
		self.assertEqual(result, "shutting_down")
		time.sleep(0.5)
		self.assertFalse(self.server.running)

	def _rpc(self, method: str, params: dict | None = None) -> object:
		"""Helper to send an RPC request."""
		if params is None:
			params = {}
		s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
		req = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		data = json.loads(resp.strip())
		if "error" in data:
			raise RuntimeError(
				f"RPC error ({data['error']['code']}): {data['error']['message']}")
		return data["result"]


class TestExifToolServerWithExiftool(unittest.TestCase):
	"""Test the server with a real exiftool subprocess.

	These tests require exiftool to be installed.
	"""

	@classmethod
	def setUpClass(cls):
		cls.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-server-real.json")
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	@classmethod
	def tearDownClass(cls):
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	def setUp(self):
		try:
			self.server = exiftool.ExifToolServer(
				port_file=self.port_file,
			)
			self.port = self.server.start()
			self.addCleanup(self.server.stop)
		except Exception as e:
			self.skipTest(f"Cannot start server with exiftool: {e}")

	def test_server_available_true(self):
		"""available() should return True when exiftool is running."""
		result = self._rpc("available")
		self.assertTrue(result)

	def test_server_execute_version(self):
		"""execute should return exiftool version."""
		result = self._rpc("execute", {"args": ["-ver"]})
		self.assertIn("stdout", result)
		self.assertIn("status", result)
		version = result["stdout"].strip()
		# Version should be a dotted number like "12.50"
		parts = version.split(".")
		self.assertGreaterEqual(len(parts), 2)
		for p in parts:
			self.assertTrue(p.isdigit(), f"Version part '{p}' is not numeric")

	def test_server_execute_json(self):
		"""execute_json should return parsed JSON metadata."""
		if not TEST_IMAGE_JPG.exists():
			self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")
		result = self._rpc("execute_json", {
			"args": ["-j", str(TEST_IMAGE_JPG)],
		})
		self.assertIsInstance(result, list)
		self.assertGreater(len(result), 0)
		self.assertIn("SourceFile", result[0])

	def test_server_get_metadata(self):
		"""get_metadata should return metadata for a file."""
		if not TEST_IMAGE_JPG.exists():
			self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")
		result = self._rpc("get_metadata", {
			"files": [str(TEST_IMAGE_JPG)],
		})
		self.assertIsInstance(result, list)
		self.assertGreater(len(result), 0)
		self.assertIn("SourceFile", result[0])

	def test_server_get_tags(self):
		"""get_tags should return specific tags."""
		if not TEST_IMAGE_JPG.exists():
			self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")
		result = self._rpc("get_tags", {
			"files": [str(TEST_IMAGE_JPG)],
			"tags": ["EXIF:DateTimeOriginal"],
		})
		self.assertIsInstance(result, list)
		self.assertGreater(len(result), 0)
		self.assertIn("SourceFile", result[0])

	def test_server_execute_with_nonzero_status(self):
		"""execute should handle non-zero exit status gracefully."""
		# Reading a non-existent tag returns exit code 1 but doesn't crash
		result = self._rpc("execute", {
			"args": ["-json", "-NonExistentTag12345", str(TEST_IMAGE_JPG)],
		})
		self.assertIn("stdout", result)
		self.assertIn("status", result)

	def _rpc(self, method: str, params: dict | None = None) -> object:
		if params is None:
			params = {}
		s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
		req = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		data = json.loads(resp.strip())
		if "error" in data:
			raise RuntimeError(
				f"RPC error ({data['error']['code']}): {data['error']['message']}")
		return data["result"]


class TestExifToolClient(unittest.TestCase):
	"""Test the ExifToolClient connecting to a server."""

	@classmethod
	def setUpClass(cls):
		cls.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-client.json")
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	@classmethod
	def tearDownClass(cls):
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	def setUp(self):
		try:
			self.server = exiftool.ExifToolServer(
				port_file=self.port_file,
			)
			self.port = self.server.start()
			self.client = exiftool.ExifToolClient(
				port=self.port, timeout=5.0)
		except Exception as e:
			self.skipTest(f"Cannot set up server: {e}")

	def tearDown(self):
		self.server.stop()
		try:
			os.unlink(self.port_file)
		except OSError:
			pass

	def test_client_context_manager(self):
		"""Client should work as a context manager."""
		with exiftool.ExifToolClient(port=self.port, timeout=5.0) as c:
			result = c.execute("-ver")
			self.assertTrue(result.strip())

	def test_client_execute(self):
		"""Client.execute() should return exiftool version."""
		result = self.client.execute("-ver")
		self.assertTrue(result.strip())

	def test_client_execute_json(self):
		"""Client.execute_json() should return parsed JSON."""
		if not TEST_IMAGE_JPG.exists():
			self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")
		result = self.client.execute_json(str(TEST_IMAGE_JPG))
		self.assertIsInstance(result, list)
		self.assertGreater(len(result), 0)
		self.assertIn("SourceFile", result[0])

	def test_client_get_metadata(self):
		"""Client.get_metadata() should work."""
		if not TEST_IMAGE_JPG.exists():
			self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")
		result = self.client.get_metadata(TEST_IMAGE_JPG)
		self.assertIsInstance(result, list)
		self.assertGreater(len(result), 0)

	def test_client_get_tags(self):
		"""Client.get_tags() should return specific tags."""
		if not TEST_IMAGE_JPG.exists():
			self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")
		result = self.client.get_tags(
			TEST_IMAGE_JPG, ["EXIF:DateTimeOriginal"])
		self.assertIsInstance(result, list)
		self.assertGreater(len(result), 0)
		self.assertIn("SourceFile", result[0])

	def test_client_running(self):
		"""Client.running should reflect server status."""
		self.assertTrue(self.client.running)
		self.server.stop()
		time.sleep(0.3)
		self.assertFalse(self.client.running)

	def test_client_auto_discover(self):
		"""Client auto-discovery via port file should work."""
		client = exiftool.ExifToolClient(
			port_file=self.port_file, timeout=5.0)
		try:
			result = client.execute("-ver")
			self.assertTrue(result.strip())
		finally:
			client.terminate()  # no-op

	def test_client_connection_refused(self):
		"""Client should raise ExifToolConnectionError for unreachable server."""
		self.server.stop()
		time.sleep(0.3)
		with self.assertRaises(ExifToolConnectionError):
			exiftool.ExifToolClient(port=self.port, timeout=2.0)


class TestExifToolPortFile(unittest.TestCase):
	"""Test the port file discovery mechanism."""

	def setUp(self):
		self.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-portfile.json")
		try:
			os.unlink(self.port_file)
		except OSError:
			pass

	def tearDown(self):
		try:
			os.unlink(self.port_file)
		except OSError:
			pass

	def test_find_server_no_file(self):
		"""find_server should return None when no port file exists."""
		result = exiftool.find_server(self.port_file)
		self.assertIsNone(result)

	def test_find_server_with_server(self):
		"""find_server should find a running server by port file."""
		server = exiftool.ExifToolServer(
			port_file=self.port_file, no_exiftool=True)
		try:
			server.start()
			port = exiftool.find_server(self.port_file, timeout=5.0)
			self.assertEqual(port, server.port)
		finally:
			server.stop()


class TestExifToolSpawnServer(unittest.TestCase):
	"""Test spawning the server as a background subprocess."""

	@classmethod
	def setUpClass(cls):
		cls.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-spawn.json")
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	@classmethod
	def tearDownClass(cls):
		try:
			os.unlink(cls.port_file)
		except OSError:
			pass

	def test_spawn_server(self):
		"""spawn_server should start a server subprocess and return its port."""
		port = exiftool.spawn_server(
			timeout=SERVER_START_TIMEOUT,
			port_file=self.port_file,
		)
		self.assertGreater(port, 0)
		# Verify it's running
		s = socket.create_connection(("127.0.0.1", port), timeout=5)
		req = json.dumps({"id": 1, "method": "ping", "params": {}}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()
		data = json.loads(resp.strip())
		self.assertEqual(data["result"], "pong")

		# Shut down
		s = socket.create_connection(("127.0.0.1", port), timeout=5)
		req = json.dumps({"id": 1, "method": "shutdown", "params": {}}) + "\n"
		s.sendall(req.encode())
		resp = s.makefile("r", encoding="utf-8").readline()
		s.close()


# ── Singleton tests ──────────────────────────────────────────────────


@unittest.skipIf(
	os.environ.get("SKIP_SINGLETON_TESPS"),
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
		# Prevent subprocess tests from eating port-file artifacts
		cls._own_pid = os.getpid()

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
		"""A second server with a higher PID must raise when the lock is held."""
		self.server.start()
		# Simulate a higher PID so we appear as the "new" process
		import unittest.mock as mock
		real_pid = os.getpid()
		fake_higher_pid = real_pid + 1000000
		with mock.patch("exiftool.server.os.getpid", return_value=fake_higher_pid):
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
		# Another server should now be able to acquire the lock
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

	def test_pid_takeover(self):
		"""A server with a lower PID takes over from a higher-PID server."""
		# Start a server in a subprocess (higher PID).  We use a short
		# idle-timeout so it exits cleanly if the takeover fails.
		import subprocess
		import sys

		sub_code = (
			"import sys; sys.path.insert(0, %r); import exiftool;"
			"srv = exiftool.ExifToolServer("
			"port_file=%r, singleton=True, no_exiftool=True,"
			"idle_timeout=30); "
			"srv.start()\n"
			"while srv.running:\n"
			"    import time; time.sleep(0.5)"
		) % (
			os.path.join(os.path.dirname(__file__), ".."),
			self.port_file,
		)
		proc = subprocess.Popen(
			[sys.executable, "-c", sub_code],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		try:
			# Wait for subprocess server to start
			time.sleep(1.5)
			self.assertIsNone(proc.poll(), "subprocess server died prematurely")

			# Now start a server from the test process (lower PID)
			takeover = exiftool.ExifToolServer(
				port_file=self.port_file,
				singleton=True,
				no_exiftool=True,
			)
			try:
				port = takeover.start()
				self.assertGreater(port, 0)

				# The old subprocess should have been shut down by the takeover
				time.sleep(2.0)
				ret = proc.poll()
				self.assertIsNotNone(ret,
					"subprocess should have been terminated by takeover")
			finally:
				takeover.stop()
		finally:
			if proc.poll() is None:
				proc.terminate()
				proc.wait()


@unittest.skipIf(
	os.environ.get("SKIP_SINGLETON_TESPS"),
	"Skipping singleton spawn tests that use subprocess",
)
class TestSpawnServerSingleton(unittest.TestCase):
	"""Test spawn_server with singleton=True."""

	@classmethod
	def setUpClass(cls):
		cls.port_file = os.path.join(
			tempfile.gettempdir(), "pyexiftool-test-spawn-singleton.json")
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

	def test_spawn_singleton_reuses_existing(self):
		"""spawn_server(singleton=True) should reuse an existing server."""
		port1 = exiftool.spawn_server(
			timeout=15.0,
			port_file=self.port_file,
		)
		self.assertGreater(port1, 0)
		try:
			port2 = exiftool.spawn_server(
				timeout=15.0,
				port_file=self.port_file,
				singleton=True,
			)
			self.assertEqual(port1, port2)
		finally:
			# Shut down via RPC
			try:
				s = socket.create_connection(("127.0.0.1", port1), timeout=5)
				req = json.dumps({"id": 1, "method": "shutdown", "params": {}}) + "\n"
				s.sendall(req.encode())
				s.close()
			except OSError:
				pass


# ---------------------------------------------------------------------------------------------------------
if __name__ == '__main__':
	unittest.main()
