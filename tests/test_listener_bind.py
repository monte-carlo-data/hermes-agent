"""
SEC-4041: the agent's HTTP listener is not reachable from outside the container
unless MCD_HTTP_LISTEN_ALL_INTERFACES opts in.

The unit tests check the bind address gunicorn.conf.py computes. The
GunicornListenerTests start the real gunicorn with the real gunicorn.conf.py
(only post_worker_init is stubbed, since it imports the agent) and a stub WSGI
app, then connect to the loopback and to a non-loopback address of this host.
They run in the Docker `tests` stage, so they exercise the image's gunicorn.
"""

import os
import runpy
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Dict, Optional
from unittest import SkipTest, TestCase, mock

GUNICORN_CONF = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"
OPT_IN = "MCD_HTTP_LISTEN_ALL_INTERFACES"


def _conf(env: Dict[str, str]) -> dict:
    base = {k: v for k, v in os.environ.items() if k not in (OPT_IN, "PORT")}
    with mock.patch.dict(os.environ, {**base, **env}, clear=True):
        return runpy.run_path(str(GUNICORN_CONF))


class BindAddressTests(TestCase):
    def test_default_binds_loopback(self):
        self.assertEqual(["127.0.0.1:8080"], _conf({})["bind"])

    def test_port_env_without_opt_in_stays_loopback(self):
        # gunicorn's own default for a set PORT is 0.0.0.0:$PORT; the config
        # must override it.
        self.assertEqual(["127.0.0.1:9090"], _conf({"PORT": "9090"})["bind"])

    def test_opt_in_binds_all_interfaces(self):
        for value in ["true", "TRUE", "1", "yes", " true "]:
            with self.subTest(value=value):
                conf = _conf({OPT_IN: value, "PORT": "9090"})
                self.assertEqual(["0.0.0.0:9090"], conf["bind"])

    def test_other_values_do_not_opt_in(self):
        for value in ["", "false", "0", "no", "on"]:
            with self.subTest(value=value):
                self.assertEqual(["127.0.0.1:8080"], _conf({OPT_IN: value})["bind"])


def _non_loopback_ipv4() -> Optional[str]:
    """This host's outbound IPv4 address (no packet is sent), or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1; connect() only picks a route
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _connects(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _gunicorn_available() -> bool:
    try:
        import gunicorn  # noqa: F401
    except ImportError:
        return False
    return True


class GunicornListenerTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if not _gunicorn_available():
            raise SkipTest("gunicorn not installed")
        cls.external_ip = _non_loopback_ipv4()
        if cls.external_ip is None:
            raise SkipTest("no non-loopback IPv4 address")
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "stub_wsgi.py").write_text(textwrap.dedent("""
                def app(environ, start_response):
                    start_response("200 OK", [("Content-Type", "text/plain")])
                    return [b"OK"]
                """))
        # The real config, with only the agent-importing hook replaced.
        cls.conf = tmp / "gunicorn_test.conf.py"
        cls.conf.write_text(
            f"exec(open({str(GUNICORN_CONF)!r}).read())\n"
            "def post_worker_init(worker):\n"
            "    pass\n"
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _start(self, env: Dict[str, str]) -> int:
        port = _free_port()
        base = {k: v for k, v in os.environ.items() if k not in (OPT_IN,)}
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "--config",
                str(self.conf),
                "--chdir",
                self._tmp.name,
                "--workers",
                "1",
                "stub_wsgi:app",
            ],
            env={**base, "PORT": str(port), **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop, proc)
        deadline = time.monotonic() + 20
        while not _connects("127.0.0.1", port):
            if proc.poll() is not None:
                self.fail(f"gunicorn exited: {proc.stderr.read().decode()}")  # type: ignore[union-attr]
            if time.monotonic() > deadline:
                self.fail("gunicorn did not start listening")
            time.sleep(0.2)
        return port

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stderr:
            proc.stderr.close()

    def test_default_not_reachable_from_outside(self):
        port = self._start({})
        self.assertTrue(_connects("127.0.0.1", port))
        self.assertFalse(
            _connects(self.external_ip, port),
            f"listener reachable on {self.external_ip}:{port} without opt-in",
        )

    def test_opt_in_reachable_from_outside(self):
        port = self._start({OPT_IN: "true"})
        self.assertTrue(_connects("127.0.0.1", port))
        self.assertTrue(_connects(self.external_ip, port))
