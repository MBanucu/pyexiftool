# -*- coding: utf-8 -*-
"""
Tests for ExifToolServer singleton lock and PID election.
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
		import subprocess

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
			deadline = time.monotonic() + 10.0
			while time.monotonic() < deadline:
				try:
					with open(self.port_file) as f:
						data = json.load(f)
					port = data["port"]
					with socket.create_connection(
						("127.0.0.1", port), timeout=1.0) as s:
						s.sendall(json.dumps(
							{"id": 1, "method": "ping",
							 "params": {}}).encode() + b"\n")
						resp = s.makefile("r", encoding="utf-8").readline()
					if resp and '"pong"' in resp:
						break
				except (OSError, KeyError, socket.timeout, ConnectionError):
					pass
				time.sleep(0.05)
			self.assertIsNone(proc.poll(), "subprocess server died prematurely")

			takeover = exiftool.ExifToolServer(
				port_file=self.port_file,
				singleton=True,
				no_exiftool=True,
			)
			try:
				port = takeover.start()
				self.assertGreater(port, 0)
				proc.wait(timeout=10.0)
			except subprocess.TimeoutExpired:
				self.fail("subprocess should have been terminated by takeover")
			finally:
				takeover.stop()
		finally:
			if proc.poll() is None:
				proc.terminate()
				proc.wait()

	def test_singleton_stress_concurrent(self):
		"""N concurrent processes with singleton=True: exactly one survives."""
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
				"    while srv.running:\n"
				"        import time; time.sleep(0.5)\n"
				"    result['status'] = 'stopped'\n"
				"except Exception as e:\n"
				"    result['status'] = 'failed'\n"
				"    result['error'] = type(e).__name__\n"
				"print(json.dumps(result))\n"
			) % (delay, root, self.port_file)
			p = subprocess.Popen(
				[sys.executable, "-c", sub_code],
				stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			)
			procs.append(p)

		deadline = time.monotonic() + 10.0
		survivors = []
		exited_results = []
		expected_winner = min(procs, key=lambda p: p.pid)

		for p in procs:
			if p is expected_winner:
				continue
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				remaining = 0.001
			try:
				out, err = p.communicate(timeout=remaining)
				exited_results.append(json.loads(out.decode()))
				for line in err.decode().splitlines():
					if "PID election" in line:
						print(f"  {line}")
			except subprocess.TimeoutExpired:
				survivors.append((p, p.pid))
				p.stdout.close()
				p.stderr.close()

		self.assertEqual(len(survivors), 0,
			f"Expected no unexpected survivors, got {len(survivors)}")
		self.assertIsNone(expected_winner.poll(),
			"Expected lowest-PID process to still be running")

		with open(self.port_file) as f:
			port_data = json.load(f)
		port = port_data["port"]
		with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
			s.sendall(json.dumps(
				{"id": 1, "method": "ping",
				 "params": {}}).encode() + b"\n")
			resp = s.makefile("r", encoding="utf-8").readline()
		self.assertIn("pong", resp or "")

		# At least one server should have been taken over (status "stopped")
		taken_over = [r for r in exited_results if r['status'] == 'stopped']
		self.assertGreater(len(taken_over), 0,
			f"No takeovers — expected at least one "
			f"({len(exited_results)} exited)")

		exited_pids = {r['pid'] for r in exited_results}
		self.assertNotIn(expected_winner.pid, exited_pids,
			f"Lowest PID {expected_winner.pid} exited prematurely "
			f"({len(exited_results)} exited)")

		surv_proc = expected_winner
		with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
			s.sendall(json.dumps(
				{"id": 1, "method": "shutdown",
				 "params": {}}).encode() + b"\n")

		out, err = surv_proc.communicate(timeout=10.0)
		surv_result = json.loads(out.decode())
		self.assertEqual(surv_result['status'], 'stopped')
		for line in err.decode().splitlines():
			if "PID election" in line:
				print(f"  {line}")


if __name__ == '__main__':
	unittest.main()
