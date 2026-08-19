import ipaddress
import json
import socket
import sys


MAX_ADDRESSES = 64


class ResolverHelperError(Exception):
    pass


def resolve_records(hostname, port):
    records = socket.getaddrinfo(
        hostname,
        port,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
    )
    addresses = []
    for record in records:
        family = record[0]
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ResolverHelperError()
        try:
            raw_address = record[4][0].split("%", 1)[0]
            address = ipaddress.ip_address(raw_address)
        except (AttributeError, IndexError, TypeError, ValueError):
            raise ResolverHelperError() from None
        if (
            family == socket.AF_INET
            and address.version != 4
            or family == socket.AF_INET6
            and address.version != 6
        ):
            raise ResolverHelperError()
        addresses.append(
            {"family": family, "address": str(address)}
        )
        if len(addresses) > MAX_ADDRESSES:
            raise ResolverHelperError()
    return {"addresses": addresses}


def main(arguments=None):
    if arguments is None:
        arguments = sys.argv[1:]
    if len(arguments) != 2:
        return 1
    try:
        port = int(arguments[1])
        if not 0 < port < 65536:
            raise ValueError()
        payload = resolve_records(arguments[0], port)
        output = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception:
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
