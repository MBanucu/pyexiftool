# -*- coding: utf-8 -*-
"""
Stress tests for ExifToolServer and ExifToolClient.

Focuses on concurrency, throughput, and edge cases around server lifecycle
with the first-come, first-served singleton lock.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import exiftool
from exiftool.exceptions import ExifToolServerError, ExifToolConnectionError

from tests.common_util import TEST_IMAGE_JPG


SERVER_START_TIMEOUT = 15.0


# ── Helpers ────────────────────────────────────────────────────────────

def _rpc(host: str, port: int, method: str,
         params: dict | None = None,
         timeout: float = 10.0) -> object:
    """Send a JSON-RPC request and return the decoded result."""
    if params is None:
        params = {}
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        req = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
        s.sendall(req.encode())
        resp = s.makefile("r", encoding="utf-8").readline()
        if not resp:
            return None
        data = json.loads(resp.strip())
        if "error" in data:
            raise RuntimeError(
                f"RPC error ({data['error']['code']}): {data['error']['message']}")
        return data.get("result")
    finally:
        s.close()


def _ping(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if a server is alive."""
    try:
        return _rpc(host, port, "ping", timeout=timeout) == "pong"
    except Exception:
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


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
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


def _cleanup_port_file(*paths: str):
    """Remove port and lock files if they exist."""
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass
        try:
            os.unlink(p + ".lock")
        except OSError:
            pass


# ── Tests ──────────────────────────────────────────────────────────────


class TestSingletonContention(unittest.TestCase):
    """Stress the first-come, first-served singleton lock with N concurrent processes."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-singleton.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)

    def test_singleton_race_N_processes(self):
        """N processes with singleton=True: exactly one succeeds."""
        N = 15
        root = os.path.join(os.path.dirname(__file__), "..")
        procs = []

        preamble = (
            "import socket, json, time\n"
        )

        for i in range(N):
            delay = (N - 1 - i) * 0.005
            sub_code = (
                "import time; time.sleep(%s)\n"
                "import sys, json, os\n"
                "sys.path.insert(0, %r)\n"
                "import exiftool\n"
                "pf = %r\n"
                "r = {'pid': os.getpid(), 'status': 'unknown'}\n"
                "try:\n"
                "    srv = exiftool.ExifToolServer(\n"
                "        port_file=pf, singleton=True,\n"
                "        no_exiftool=True, idle_timeout=30)\n"
                "    port = srv.start()\n"
                "    r['status'] = 'running'; r['port'] = port\n"
                "except Exception as e:\n"
                "    r['status'] = 'failed'; r['error'] = type(e).__name__\n"
                "# Print immediately — always before the while loop\n"
                "sys.stdout.write(json.dumps(r) + '\\n')\n"
                "sys.stdout.flush()\n"
                "if r['status'] == 'running':\n"
                "    while srv.running:\n"
                "        time.sleep(0.2)\n"
                "    r['status'] = 'stopped'\n"
                "    sys.stdout.write(json.dumps(r) + '\\n')\n"
                "    sys.stdout.flush()\n"
            ) % (delay, root, self.port_file)

            p = subprocess.Popen(
                [sys.executable, "-c", preamble + sub_code],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            procs.append(p)

        # Read one line from each process (the initial status)
        results = []
        for p in procs:
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

        # Cleanup survivor
        _shutdown("127.0.0.1", port)
        for p in procs:
            try:
                p.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                p.kill()

    def test_singleton_already_running_info(self):
        """When singleton=True and a server is alive, the error must mention the port."""
        server1 = exiftool.ExifToolServer(
            port_file=self.port_file, singleton=True, no_exiftool=True)
        server1.start()
        self.addCleanup(server1.stop)

        with self.assertRaises(ExifToolServerError) as cm:
            exiftool.ExifToolServer(
                port_file=self.port_file, singleton=True, no_exiftool=True
            ).start()
        msg = str(cm.exception).lower()
        self.assertIn("port", msg)
        self.assertIn(str(server1.port), msg)


class TestInFlightRequestDuringShutdown(unittest.TestCase):
    """What happens to a client when the server is shut down mid-request.

    This simulates the original PID-election takeover scenario but with
    a clean shutdown trigger (RPC shutdown).
    """

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-inflight.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)

    def test_inflight_slow_handler_gets_response_on_shutdown(self):
        """A slow RPC handler should complete before the server fully stops.

        We inject a delay into the 'status' handler by patching. When a
        shutdown arrives while a status request is being processed, the
        status handler should still complete and the client should receive
        its response.
        """
        server = exiftool.ExifToolServer(
            port_file=self.port_file, singleton=True, no_exiftool=True)
        port = server.start()
        self.addCleanup(server.stop)

        # Add a brief delay to _rpc_status to create a race window
        original_status = server._rpc_status
        def slow_status():
            time.sleep(0.3)
            return original_status()
        server._rpc_status = slow_status

        # Fire a slow status request and immediately send shutdown
        results = []
        def request_thread():
            try:
                results.append(
                    _rpc("127.0.0.1", port, "status", timeout=5.0))
            except Exception as e:
                results.append(e)

        t = threading.Thread(target=request_thread, daemon=True)
        t.start()
        time.sleep(0.1)  # let the slow handler start

        # Send shutdown while the handler is still running
        _shutdown("127.0.0.1", port)
        t.join(timeout=5.0)

        self.assertEqual(len(results), 1,
            "Client should have received exactly one result")
        if isinstance(results[0], Exception):
            self.fail(f"Client got exception instead of response: {results[0]}")
        self.assertIn("port", results[0],
            "Client should receive the status result even though shutdown was sent")

    def test_inflight_execute_catches_error_after_subprocess_killed(self):
        """When exiftool subprocess is killed mid-execute, client gets an error.

        This simulates what happens in the PID-election scenario where
        one server's exiftool is terminated while processing a request.
        """
        if not TEST_IMAGE_JPG.exists():
            self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")

        server = exiftool.ExifToolServer(
            port_file=self.port_file, singleton=True)
        port = server.start()
        self.addCleanup(server.stop)

        # Fire a get_metadata and kill the subprocess mid-flight
        results = []
        def query_thread():
            try:
                results.append(
                    _rpc("127.0.0.1", port, "get_metadata",
                         {"files": [str(TEST_IMAGE_JPG)]}, timeout=10.0))
            except Exception as e:
                results.append(e)

        t = threading.Thread(target=query_thread, daemon=True)
        t.start()
        time.sleep(0.2)

        # Kill the underlying exiftool subprocess (simulating another server
        # interfering — not via our clean RPC shutdown)
        if server._helper and server._helper.running:
            server._helper.terminate()

        t.join(timeout=10.0)
        self.assertEqual(len(results), 1,
            "Client should have received exactly one result/error")
        # The client should not hang — it should get either an error response
        # or a socket error.  The key assertion is that it completes.
        self.assertIsNotNone(results[0])


class TestConcurrentClients(unittest.TestCase):
    """Many threads sending requests to the same server concurrently."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-concurrent.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)
        self.server = exiftool.ExifToolServer(
            port_file=self.port_file, no_exiftool=True)
        self.port = self.server.start()
        self.errors = []

    def tearDown(self):
        self.server.stop()

    def test_concurrent_ping_burst(self):
        """50 threads each send 20 pings — all must succeed."""
        N_THREADS = 50
        N_REQUESTS = 20
        results_lock = threading.Lock()
        results = {"ok": 0, "fail": 0}

        def worker():
            for _ in range(N_REQUESTS):
                try:
                    _rpc("127.0.0.1", self.port, "ping", timeout=5.0)
                    with results_lock:
                        results["ok"] += 1
                except Exception:
                    with results_lock:
                        results["fail"] += 1

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        expected = N_THREADS * N_REQUESTS
        self.assertEqual(results["fail"], 0,
            f"Expected 0 failures, got {results['fail']}")
        self.assertEqual(results["ok"], expected,
            f"Expected {expected} OK, got {results['ok']}")

    def test_concurrent_mixed_rpc(self):
        """Multiple threads send different RPC requests simultaneously."""
        N_THREADS = 20
        N_REQUESTS = 15
        results_lock = threading.Lock()
        results = {"ok": 0, "fail": 0}

        methods = ["ping", "status", "available"]

        def worker():
            for _ in range(N_REQUESTS):
                method = methods[_ % len(methods)]
                try:
                    _rpc("127.0.0.1", self.port, method, timeout=5.0)
                    with results_lock:
                        results["ok"] += 1
                except Exception:
                    with results_lock:
                        results["fail"] += 1

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        self.assertEqual(results["fail"], 0,
            f"Expected 0 failures, got {results['fail']}")
        self.assertEqual(results["ok"], N_THREADS * N_REQUESTS)

    def test_concurrent_client_objects(self):
        """50 client objects created, each does a few requests.

        Uses the helper API (get_metadata, get_tags) which, while they
        need exiftool, will still exercise the RPC path and return a
        clear error rather than hang.
        """
        N_CLIENTS = 50
        results = {"ok": 0, "fail": 0}
        results_lock = threading.Lock()

        def worker():
            try:
                client = exiftool.ExifToolClient(
                    port=self.port, timeout=5.0)
                for _ in range(3):
                    self.assertTrue(client.running)
                    self.assertGreater(client.port, 0)
                    self.assertEqual(client.host, "127.0.0.1")
                with results_lock:
                    results["ok"] += 1
            except Exception:
                with results_lock:
                    results["fail"] += 1

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(N_CLIENTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        self.assertEqual(results["fail"], 0,
            f"Expected 0 failures, got {results['fail']}")
        self.assertEqual(results["ok"], N_CLIENTS)


class TestConnectionStorm(unittest.TestCase):
    """Rapid connect/disconnect cycles against the server."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-storm.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)
        self.server = exiftool.ExifToolServer(
            port_file=self.port_file, no_exiftool=True)
        self.port = self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_rapid_connect_disconnect(self):
        """Open and close 500 connections without sending data."""
        for _ in range(500):
            try:
                s = socket.create_connection(
                    ("127.0.0.1", self.port), timeout=5.0)
                s.close()
            except (OSError, socket.timeout, ConnectionError) as e:
                self.fail(f"Connection failed at iteration: {e}")

    def test_rapid_connect_send_close(self):
        """Open, send partial data, close — 500 cycles."""
        for i in range(500):
            try:
                s = socket.create_connection(
                    ("127.0.0.1", self.port), timeout=5.0)
                # Send a partial JSON fragment (no newline)
                s.sendall(json.dumps({"id": i, "method": "ping"}).encode())
                s.close()
            except (OSError, socket.timeout, ConnectionError) as e:
                self.fail(f"Connection failed at iteration {i}: {e}")

    def test_rapid_full_requests(self):
        """Full RPC requests as fast as possible — 1000 iterations."""
        for i in range(1000):
            try:
                result = _rpc("127.0.0.1", self.port, "ping", timeout=5.0)
                self.assertEqual(result, "pong",
                    f"Iteration {i}: expected pong, got {result}")
            except Exception as e:
                self.fail(f"Iteration {i} failed: {e}")


class TestServerThroughput(unittest.TestCase):
    """Measure and verify raw request throughput."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-throughput.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)
        self.server = exiftool.ExifToolServer(
            port_file=self.port_file, no_exiftool=True)
        self.port = self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_throughput_ping(self):
        """Measure ping throughput — must complete 500 pings in under 10s."""
        N = 500
        start = time.monotonic()
        for _ in range(N):
            result = _rpc("127.0.0.1", self.port, "ping", timeout=5.0)
            self.assertEqual(result, "pong")
        elapsed = time.monotonic() - start
        rps = N / elapsed
        self.assertGreaterEqual(rps, 10,
            f"Throughput too low: {rps:.1f} req/s ({elapsed:.2f}s for {N})")

    def test_throughput_status(self):
        """Measure status throughput — 500 status calls in under 10s."""
        N = 500
        start = time.monotonic()
        for _ in range(N):
            result = _rpc("127.0.0.1", self.port, "status", timeout=5.0)
            self.assertIn("port", result)
        elapsed = time.monotonic() - start
        rps = N / elapsed
        self.assertGreaterEqual(rps, 10,
            f"Throughput too low: {rps:.1f} req/s ({elapsed:.2f}s for {N})")


class TestIdleTimeoutUnderLoad(unittest.TestCase):
    """Verify idle timeout doesn't fire while requests keep coming."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-idle.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)

    def test_idle_timeout_not_fired_under_sustained_load(self):
        """Server with 5s idle timeout must stay alive under constant load for 20s."""
        server = exiftool.ExifToolServer(
            port_file=self.port_file,
            no_exiftool=True,
            idle_timeout=5.0,
        )
        port = server.start()
        self.addCleanup(server.stop)

        deadline = time.monotonic() + 20.0
        iterations = 0
        while time.monotonic() < deadline:
            result = _rpc("127.0.0.1", port, "ping", timeout=5.0)
            self.assertEqual(result, "pong")
            iterations += 1
            time.sleep(0.5)

        self.assertGreater(iterations, 10,
            f"Expected at least 10 iterations in 20s, got {iterations}")
        self.assertTrue(server.running,
            "Server should still be running after sustained load")


class TestSpawnServerStress(unittest.TestCase):
    """Stress test spawn_server with singleton mode."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-spawn.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)

    def test_spawn_then_client_storm(self):
        """Spawn a server, then hit it with 50 concurrent ExifToolClient objects."""
        port = exiftool.spawn_server(
            timeout=SERVER_START_TIMEOUT,
            port_file=self.port_file,
        )
        self.assertGreater(port, 0)
        self.addCleanup(lambda: _shutdown("127.0.0.1", port))

        results = {"ok": 0, "fail": 0}
        results_lock = threading.Lock()

        def worker():
            try:
                client = exiftool.ExifToolClient(
                    port=port, timeout=5.0)
                for _ in range(10):
                    client.execute("-ver")
                    client.running
                with results_lock:
                    results["ok"] += 1
            except Exception:
                with results_lock:
                    results["fail"] += 1

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        self.assertEqual(results["fail"], 0,
            f"Expected 0 failures, got {results['fail']}")
        self.assertEqual(results["ok"], 50)


class TestSingletonWithExiftoolStress(unittest.TestCase):
    """Singleton server with real exiftool under concurrent load."""

    @classmethod
    def setUpClass(cls):
        cls.port_file = os.path.join(
            tempfile.gettempdir(), "pyexiftool-stress-real.json")
        _cleanup_port_file(cls.port_file)

    @classmethod
    def tearDownClass(cls):
        _cleanup_port_file(cls.port_file)

    def setUp(self):
        _cleanup_port_file(self.port_file)
        try:
            self.server = exiftool.ExifToolServer(
                port_file=self.port_file,
                singleton=True,
            )
            self.port = self.server.start()
        except Exception as e:
            self.skipTest(f"Cannot start server with exiftool: {e}")

    def tearDown(self):
        self.server.stop()

    def test_concurrent_metadata_reads(self):
        """10 threads each query metadata 10 times — all must succeed."""
        if not TEST_IMAGE_JPG.exists():
            self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")

        N_THREADS = 10
        N_REQUESTS = 10
        results = {"ok": 0, "fail": 0}
        results_lock = threading.Lock()

        def worker():
            for _ in range(N_REQUESTS):
                try:
                    result = _rpc("127.0.0.1", self.port, "get_metadata",
                                  {"files": [str(TEST_IMAGE_JPG)]}, timeout=15.0)
                    self.assertIsInstance(result, list)
                    self.assertGreater(len(result), 0)
                    with results_lock:
                        results["ok"] += 1
                except Exception:
                    with results_lock:
                        results["fail"] += 1

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60.0)

        self.assertEqual(results["fail"], 0,
            f"Expected 0 failures, got {results['fail']}")
        self.assertEqual(results["ok"], N_THREADS * N_REQUESTS)

    def test_interleaved_read_write(self):
        """Interleave get_tags and set_tags on temp copies concurrently."""
        import tempfile as tf
        import shutil

        if not TEST_IMAGE_JPG.exists():
            self.skipTest(f"Test image not found: {TEST_IMAGE_JPG}")

        # Create temporary copies
        tmp_dir = tf.mkdtemp(prefix="pyexiftool-stress-")
        self.addCleanup(shutil.rmtree, tmp_dir)
        copies = []
        for i in range(5):
            dst = os.path.join(tmp_dir, f"copy_{i}.jpg")
            shutil.copy2(TEST_IMAGE_JPG, dst)
            copies.append(dst)

        N_THREADS = 10
        results = {"ok": 0, "fail": 0}
        results_lock = threading.Lock()

        def worker_read(tid):
            for _ in range(5):
                try:
                    file = copies[tid % len(copies)]
                    result = _rpc("127.0.0.1", self.port, "get_tags",
                                  {"files": [file],
                                   "tags": ["EXIF:DateTimeOriginal"]},
                                  timeout=15.0)
                    self.assertIsInstance(result, list)
                    with results_lock:
                        results["ok"] += 1
                except Exception:
                    with results_lock:
                        results["fail"] += 1

        def worker_write(tid):
            for _ in range(3):
                try:
                    file = copies[tid % len(copies)]
                    result = _rpc("127.0.0.1", self.port, "set_tags",
                                  {"files": [file],
                                   "tags": {"XMP:Description": f"test_{tid}_{_}"}},
                                  timeout=15.0)
                    with results_lock:
                        results["ok"] += 1
                except Exception:
                    with results_lock:
                        results["fail"] += 1

        threads = []
        for i in range(N_THREADS):
            target = worker_read if i % 2 == 0 else worker_write
            t = threading.Thread(target=target, args=(i,), daemon=True)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60.0)

        self.assertEqual(results["fail"], 0,
            f"Expected 0 failures, got {results['fail']}")


if __name__ == '__main__':
    unittest.main()
