"""Check the TLS certificate a site actually serves: valid, trusted, and not expiring soon.

Usage (from the project root; standard library only):

    python -m scripts.check_certificate support.example.com
    python -m scripts.check_certificate support.example.com --warn-days 21
    python -m scripts.check_certificate localhost --port 18443 \
        --ca-file deploy/nginx/certs/fullchain.pem      # a development certificate

It connects like a browser would: the chain must lead to a trusted certificate
authority (the system's, or --ca-file) and the certificate must cover the hostname. It
then reports the issuer, the names covered and the days left.

Exit codes, for cron jobs and monitoring systems:
    0  valid for more than --warn-days
    1  valid, but expires within --warn-days: renewal has not happened
    2  invalid (untrusted, wrong name, expired) or the site could not be reached

Let's Encrypt no longer emails expiry reminders, and its certificates are short-lived,
so check from outside the server, every day (docs/https-setup.md, "Monitoring").
Prints no secrets; a certificate is public.
"""

import argparse
import socket
import ssl
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_PORT = 443
DEFAULT_WARN_DAYS = 21
DEFAULT_TIMEOUT_SECONDS = 10.0
SECONDS_PER_DAY = 86_400

OK, EXPIRING, INVALID = 0, 1, 2


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    issuer: str
    names: tuple[str, ...]
    not_after: str  # as the certificate states it, e.g. "Jan 19 12:00:00 2027 GMT"
    expires_at: float  # seconds since the epoch


def describe(certificate: dict) -> CertificateInfo:
    """The fields that matter, from ``SSLSocket.getpeercert()``."""

    def field(name: str) -> str:
        for rdn in certificate.get(name, ()):
            for key, value in rdn:
                if key in ("commonName", "organizationName"):
                    return value
        return "-"

    names = tuple(value for kind, value in certificate.get("subjectAltName", ()) if kind == "DNS")
    not_after = certificate["notAfter"]
    return CertificateInfo(
        subject=field("subject"),
        issuer=field("issuer"),
        names=names,
        not_after=not_after,
        expires_at=ssl.cert_time_to_seconds(not_after),
    )


def days_left(info: CertificateInfo, now: float) -> float:
    return (info.expires_at - now) / SECONDS_PER_DAY


def verdict(info: CertificateInfo, now: float, warn_days: int) -> int:
    remaining = days_left(info, now)
    if remaining <= 0:
        return INVALID
    return EXPIRING if remaining <= warn_days else OK


def fetch(
    host: str, port: int, *, ca_file: str | None, timeout: float, server_name: str | None = None
) -> CertificateInfo:
    """Connect with full verification (chain and hostname) and return the certificate.

    Raises:
        ssl.SSLError, ssl.CertificateError: untrusted, expired or wrong name.
        OSError: the host could not be reached.
    """
    context = ssl.create_default_context(cafile=ca_file)
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=server_name or host) as tls:
            certificate = tls.getpeercert()
    if not certificate:  # only possible without verification, which is never disabled here
        raise ssl.SSLError("no certificate received")
    return describe(certificate)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="Hostname to check, as clients use it.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--warn-days",
        type=int,
        default=DEFAULT_WARN_DAYS,
        help=f"Exit 1 when fewer days remain (default {DEFAULT_WARN_DAYS}).",
    )
    parser.add_argument(
        "--ca-file",
        help="Trust this CA (or self-signed development certificate) instead of the system's.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        info = fetch(args.host, args.port, ca_file=args.ca_file, timeout=args.timeout)
    except (ssl.SSLError, ssl.CertificateError) as exc:
        print(f"INVALID {args.host}:{args.port}: {exc.__class__.__name__}: {exc}")
        return INVALID
    except OSError as exc:
        print(f"UNREACHABLE {args.host}:{args.port}: {exc.__class__.__name__}: {exc}")
        return INVALID

    now = time.time()
    status = verdict(info, now, args.warn_days)
    label = {OK: "OK", EXPIRING: "RENEW SOON", INVALID: "EXPIRED"}[status]
    print(
        f"{label} {args.host}:{args.port}: {days_left(info, now):.1f} days left "
        f"(until {info.not_after}); issuer {info.issuer}; names {', '.join(info.names) or '-'}"
    )
    return status


if __name__ == "__main__":
    sys.exit(main())
