import importlib
import ipaddress
import json
import socket
import subprocess
import sys
from time import monotonic
from unittest import mock

from django.test import SimpleTestCase

from core.services import bounded_resolver, resolver_helper
from core.services.bounded_resolver import BoundedResolver


class ManualClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class CompletedProcess:
    def __init__(self, output, returncode=0):
        self.output = output
        self.returncode = returncode
        self.communicate_calls = []
        self.terminated = False
        self.killed = False

    def attach_stdout(self, stdout):
        stdout.write(self.output)
        stdout.flush()

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        return None, None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class TimeoutProcess(CompletedProcess):
    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if len(self.communicate_calls) <= 2:
            raise subprocess.TimeoutExpired("resolver", timeout)
        return None, None


class RecordingPopenFactory:
    def __init__(self, process):
        self.process = process
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.process.attach_stdout(kwargs["stdout"])
        return self.process


def resolver_payload(addresses):
    return json.dumps({"addresses": addresses}).encode("ascii")


class BoundedResolverTests(SimpleTestCase):
    def test_import_creates_no_child_process(self):
        with mock.patch("subprocess.Popen") as popen:
            importlib.reload(bounded_resolver)

        popen.assert_not_called()

    def test_uses_fixed_python_module_command_without_shell(self):
        process = CompletedProcess(
            resolver_payload(
                [
                    {"family": socket.AF_INET, "address": "203.0.113.10"},
                    {"family": socket.AF_INET6, "address": "2001:db8::10"},
                ]
            )
        )
        factory = RecordingPopenFactory(process)
        resolver = BoundedResolver(popen_factory=factory)
        deadline = monotonic() + 5

        addresses = resolver.resolve("images.test", 8443, deadline)

        self.assertEqual(addresses, ["203.0.113.10", "2001:db8::10"])
        command, kwargs = factory.calls[0]
        self.assertEqual(
            command,
            [
                sys.executable,
                "-m",
                "core.services.resolver_helper",
                "images.test",
                "8443",
            ],
        )
        self.assertNotIn("shell", kwargs)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertGreater(process.communicate_calls[0], 0)
        self.assertLessEqual(process.communicate_calls[0], 5)

    def test_expired_deadline_does_not_spawn_process(self):
        factory = mock.Mock()
        resolver = BoundedResolver(
            popen_factory=factory,
            clock=lambda: 10.0,
        )

        with self.assertRaises(TimeoutError):
            resolver.resolve("images.test", 443, deadline=10.0)

        factory.assert_not_called()

    def test_timeout_terminates_then_kills_and_reaps_child(self):
        process = TimeoutProcess(b"")
        factory = RecordingPopenFactory(process)
        resolver = BoundedResolver(
            popen_factory=factory,
            clock=lambda: 10.0,
            terminate_timeout=0.05,
        )

        with self.assertRaises(TimeoutError):
            resolver.resolve("images.test", 443, deadline=10.01)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(
            process.communicate_calls,
            [0.009999999999999787, 0.05, None],
        )

    def test_deadline_consumed_during_spawn_terminates_child(self):
        clock = ManualClock(10.0)
        process = CompletedProcess(
            resolver_payload(
                [{"family": socket.AF_INET, "address": "203.0.113.10"}]
            )
        )
        base_factory = RecordingPopenFactory(process)

        def slow_factory(command, **kwargs):
            child = base_factory(command, **kwargs)
            clock.now = 11.0
            return child

        resolver = BoundedResolver(
            popen_factory=slow_factory,
            clock=clock,
            terminate_timeout=0.05,
        )

        with self.assertRaises(TimeoutError):
            resolver.resolve("images.test", 443, deadline=10.5)

        self.assertTrue(process.terminated)
        self.assertEqual(process.communicate_calls, [0.05])

    def test_rejects_oversized_or_malformed_helper_output(self):
        malformed_outputs = (
            b"x" * (64 * 1024 + 1),
            b"not json",
            b"[]",
            b'{"addresses": [], "extra": true}',
            b'{"addresses": "203.0.113.10"}',
            resolver_payload([{"family": 999, "address": "203.0.113.10"}]),
            resolver_payload([{"family": socket.AF_INET}]),
            resolver_payload(
                [{"family": socket.AF_INET, "address": "2001:db8::10"}]
            ),
            resolver_payload(
                [
                    {"family": socket.AF_INET, "address": "203.0.113.10"}
                ]
                * 65
            ),
        )

        for output in malformed_outputs:
            with self.subTest(output=output[:40]):
                process = CompletedProcess(output)
                resolver = BoundedResolver(
                    popen_factory=RecordingPopenFactory(process)
                )
                with self.assertRaises(Exception) as caught:
                    resolver.resolve(
                        "images.test", 443, monotonic() + 5
                    )
                self.assertNotIsInstance(caught.exception, TimeoutError)
                self.assertNotIn("203.0.113.10", str(caught.exception))

    def test_nonzero_helper_exit_is_fail_closed(self):
        process = CompletedProcess(b"internal resolver detail", returncode=1)
        resolver = BoundedResolver(
            popen_factory=RecordingPopenFactory(process)
        )

        with self.assertRaises(Exception) as caught:
            resolver.resolve("images.test", 443, monotonic() + 5)

        self.assertNotIn("internal resolver detail", str(caught.exception))

    def test_real_helper_preserves_localhost_system_resolution(self):
        addresses = BoundedResolver().resolve(
            "localhost", 443, monotonic() + 5
        )

        self.assertTrue(addresses)
        self.assertTrue(
            all(ipaddress.ip_address(address).is_loopback for address in addresses)
        )


class ResolverHelperTests(SimpleTestCase):
    def test_getaddrinfo_is_called_once_with_tcp_constraints(self):
        records = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.10", 443),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2001:db8::10", 443, 0, 0),
            ),
        ]
        with mock.patch.object(
            resolver_helper.socket,
            "getaddrinfo",
            return_value=records,
        ) as getaddrinfo:
            result = resolver_helper.resolve_records("images.test", 443)

        getaddrinfo.assert_called_once_with(
            "images.test",
            443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        self.assertEqual(
            result,
            {
                "addresses": [
                    {"family": socket.AF_INET, "address": "203.0.113.10"},
                    {"family": socket.AF_INET6, "address": "2001:db8::10"},
                ]
            },
        )

    def test_unexpected_address_family_is_rejected(self):
        records = [
            (
                socket.AF_UNIX,
                socket.SOCK_STREAM,
                0,
                "",
                "/tmp/not-an-ip",
            )
        ]
        with mock.patch.object(
            resolver_helper.socket,
            "getaddrinfo",
            return_value=records,
        ):
            with self.assertRaises(Exception):
                resolver_helper.resolve_records("images.test", 443)
