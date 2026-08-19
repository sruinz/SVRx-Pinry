import ipaddress
import json
import socket
import subprocess
import sys
import tempfile
from time import monotonic


MAX_OUTPUT_BYTES = 64 * 1024
MAX_ADDRESSES = 64


class BoundedResolverError(Exception):
    def __init__(self):
        super(BoundedResolverError, self).__init__(
            "Name resolution failed."
        )


class BoundedResolver:
    def __init__(
        self,
        popen_factory=subprocess.Popen,
        clock=monotonic,
        terminate_timeout=0.1,
    ):
        self.popen_factory = popen_factory
        self.clock = clock
        self.terminate_timeout = terminate_timeout

    def resolve(self, hostname, port, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError()
        command = [
            sys.executable,
            "-m",
            "core.services.resolver_helper",
            hostname,
            str(port),
        ]
        with tempfile.TemporaryFile() as stdout_file:
            try:
                process = self.popen_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except Exception:
                raise BoundedResolverError() from None
            remaining = deadline - self.clock()
            if remaining <= 0:
                self._terminate(process)
                raise TimeoutError()
            try:
                process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                raise TimeoutError() from None
            except Exception:
                self._terminate(process)
                raise BoundedResolverError() from None
            if process.returncode != 0:
                raise BoundedResolverError()
            output = self._read_output(stdout_file)
        return self._parse_output(output)

    def _terminate(self, process):
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.communicate(timeout=self.terminate_timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.communicate(timeout=self.terminate_timeout)
        except Exception:
            pass

    @staticmethod
    def _read_output(stdout_file):
        stdout_file.seek(0, 2)
        size = stdout_file.tell()
        if size > MAX_OUTPUT_BYTES:
            raise BoundedResolverError()
        stdout_file.seek(0)
        return stdout_file.read(MAX_OUTPUT_BYTES + 1)

    @staticmethod
    def _parse_output(output):
        try:
            payload = json.loads(output.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            raise BoundedResolverError() from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"addresses"}
            or not isinstance(payload["addresses"], list)
            or len(payload["addresses"]) > MAX_ADDRESSES
        ):
            raise BoundedResolverError()
        addresses = []
        for record in payload["addresses"]:
            if (
                not isinstance(record, dict)
                or set(record) != {"family", "address"}
                or type(record["family"]) is not int
                or record["family"] not in (
                    socket.AF_INET,
                    socket.AF_INET6,
                )
                or not isinstance(record["address"], str)
            ):
                raise BoundedResolverError()
            try:
                address = ipaddress.ip_address(record["address"])
            except ValueError:
                raise BoundedResolverError() from None
            if (
                record["family"] == socket.AF_INET
                and address.version != 4
                or record["family"] == socket.AF_INET6
                and address.version != 6
            ):
                raise BoundedResolverError()
            addresses.append(str(address))
        return addresses
