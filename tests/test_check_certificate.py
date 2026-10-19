"""scripts/check_certificate.py: certificate expiry monitoring (Stage 13)."""

import os
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import check_certificate as check

NOW = ssl.cert_time_to_seconds("Oct 19 12:00:00 2026 GMT")
PEER_CERT = {
    "subject": ((("commonName", "support.example.com"),),),
    "issuer": ((("countryName", "US"),), (("organizationName", "Let's Encrypt"),)),
    "subjectAltName": (("DNS", "support.example.com"), ("DNS", "admin.example.com")),
    "notAfter": "Nov 18 12:00:00 2026 GMT",  # 30 days after NOW
}


def test_describe_reads_names_issuer_and_expiry() -> None:
    info = check.describe(PEER_CERT)

    assert info.subject == "support.example.com"
    assert info.issuer == "Let's Encrypt"
    assert info.names == ("support.example.com", "admin.example.com")
    assert check.days_left(info, NOW) == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("not_after", "expected"),
    [
        ("Nov 18 12:00:00 2026 GMT", check.OK),  # 30 days
        ("Nov 09 12:00:00 2026 GMT", check.EXPIRING),  # 21 days: renewal overdue
        ("Oct 20 12:00:00 2026 GMT", check.EXPIRING),  # 1 day
        ("Oct 19 11:00:00 2026 GMT", check.INVALID),  # expired an hour ago
    ],
)
def test_verdict_by_days_left(not_after: str, expected: int) -> None:
    info = check.describe({**PEER_CERT, "notAfter": not_after})

    assert check.verdict(info, NOW, warn_days=21) == expected


# --- Against a real TLS server on 127.0.0.1 ------------------------------------------------

OPENSSL = shutil.which("openssl")


def make_certificate(folder: Path, days: int) -> tuple[Path, Path]:
    """A self-signed certificate for "localhost" (test only), made with the openssl CLI."""
    cert, key = folder / "cert.pem", folder / "key.pem"
    # Git for Windows' openssl would otherwise rewrite "/O=..." as a file path.
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}
    subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [
            str(OPENSSL), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", str(days), "-subj", "/O=Test only/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True, capture_output=True, env=env,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    """A TLS server on 127.0.0.1 presenting a 30-day test certificate."""
    if OPENSSL is None:
        pytest.skip("openssl command not available")
    cert, key = make_certificate(tmp_path, days=30)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass  # clients that reject the certificate close the handshake

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], cert
    stop.set()
    thread.join(timeout=2)
    listener.close()


def run(port: int, *args: str) -> int:
    return check.main(["localhost", "--port", str(port), "--timeout", "5", *args])


def test_a_trusted_certificate_with_time_left_is_ok(
    tls_server: tuple[int, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    port, cert = tls_server

    assert run(port, "--ca-file", str(cert), "--warn-days", "10") == check.OK
    out = capsys.readouterr().out
    assert out.startswith("OK localhost") and "names localhost" in out


def test_a_certificate_inside_the_warning_window_asks_for_renewal(
    tls_server: tuple[int, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    port, cert = tls_server

    assert run(port, "--ca-file", str(cert), "--warn-days", "60") == check.EXPIRING
    assert capsys.readouterr().out.startswith("RENEW SOON")


def test_an_untrusted_certificate_is_invalid(
    tls_server: tuple[int, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    port, _ = tls_server

    assert run(port) == check.INVALID  # self-signed: not in the system's trust store
    assert capsys.readouterr().out.startswith("INVALID")


def test_a_certificate_for_another_name_is_invalid(
    tls_server: tuple[int, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    port, cert = tls_server

    code = check.main(["127.0.0.1", "--port", str(port), "--ca-file", str(cert), "--timeout", "5"])

    assert code == check.INVALID  # covers "localhost" only
    assert "INVALID" in capsys.readouterr().out


def test_an_unreachable_site_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listens there now

    assert run(port, "--timeout", "2") == check.INVALID
    assert capsys.readouterr().out.startswith("UNREACHABLE")
