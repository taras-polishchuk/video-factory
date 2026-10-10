"""In-process fake SMTP server for the EMAIL E1 tests (stdlib only).

aiosmtpd is not installed and ``smtpd`` is gone in Python 3.12, so
this is a small threaded line-protocol server with just enough SMTP
for ``smtplib``: EHLO/HELO, STARTTLS, implicit TLS, AUTH PLAIN/LOGIN,
MAIL/RCPT/DATA/RSET/NOOP/QUIT, plus fault injection (421 greeting for
the first N connections, fixed RCPT reply code, STARTTLS not offered).

``make_tls_material(dir)`` writes a throw-away CA + localhost leaf
certificate (cryptography, already a dependency) so the client side
can verify TLS for real through ``SMTP_CA_FILE``.
"""
from __future__ import annotations

import base64
import datetime as _dt
import ipaddress
import re
import socket
import socketserver
import ssl
import threading
from email import message_from_bytes, policy
from pathlib import Path
from typing import Optional


def make_tls_material(directory: Path) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vf-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
            ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_file = directory / "ca.pem"
    cert_file = directory / "server.pem"
    key_file = directory / "server.key"
    ca_file.write_bytes(ca_cert.public_bytes(pem))
    cert_file.write_bytes(leaf_cert.public_bytes(pem))
    key_file.write_bytes(leaf_key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return {"ca": str(ca_file), "cert": str(cert_file), "key": str(key_file)}


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):  # noqa: C901 - protocol state machine
        fake: FakeSMTPServer = self.server.fake  # type: ignore[attr-defined]
        sock = self.request
        sock.settimeout(10)
        with fake.lock:
            fake.connections += 1
            n = fake.connections
        tls = False
        try:
            if fake.mode == "ssl":
                sock = fake.server_ctx.wrap_socket(sock, server_side=True)
                tls = True
            f = sock.makefile("rwb")

            def send(line: str) -> None:
                f.write((line + "\r\n").encode())
                f.flush()

            if n <= fake.fail_greeting_times:
                send("421 4.3.2 Service not available, try later")
                return
            send("220 fake.smtp ESMTP ready")
            mail_from: Optional[str] = None
            rcpts: list[str] = []
            auth_user: Optional[str] = None
            while True:
                raw = f.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                up = line.upper()
                with fake.lock:
                    fake.commands.append(line if not up.startswith("AUTH") else "AUTH ***")
                if up.startswith(("EHLO", "HELO")):
                    caps = ["fake.smtp"]
                    if fake.mode == "starttls" and not tls and fake.offer_starttls:
                        caps.append("STARTTLS")
                    if fake.auth is not None:
                        caps.append("AUTH PLAIN LOGIN")
                    caps.append("8BITMIME")
                    for i, c in enumerate(caps):
                        send(("250 " if i == len(caps) - 1 else "250-") + c)
                elif up == "STARTTLS":
                    if fake.mode != "starttls" or not fake.offer_starttls or tls:
                        send("502 5.5.1 STARTTLS not available")
                        continue
                    send("220 2.0.0 Ready to start TLS")
                    f.close()
                    sock = fake.server_ctx.wrap_socket(sock, server_side=True)
                    tls = True
                    f = sock.makefile("rwb")
                elif up.startswith("AUTH PLAIN"):
                    parts = line.split(" ", 2)
                    if len(parts) == 3:
                        payload = parts[2]
                    else:
                        send("334 ")
                        payload = f.readline().decode().strip()
                    try:
                        _, user, pw = base64.b64decode(payload).decode().split("\0")
                    except Exception:  # noqa: BLE001
                        send("501 5.5.2 bad AUTH payload")
                        continue
                    if fake.auth == (user, pw):
                        auth_user = user
                        send("235 2.7.0 Authentication successful")
                    else:
                        send("535 5.7.8 Authentication credentials invalid")
                elif up.startswith("AUTH LOGIN"):
                    send("334 " + base64.b64encode(b"Username:").decode())
                    user = base64.b64decode(f.readline().strip()).decode()
                    send("334 " + base64.b64encode(b"Password:").decode())
                    pw = base64.b64decode(f.readline().strip()).decode()
                    if fake.auth == (user, pw):
                        auth_user = user
                        send("235 2.7.0 Authentication successful")
                    else:
                        send("535 5.7.8 Authentication credentials invalid")
                elif up.startswith("MAIL FROM:"):
                    if fake.auth is not None and auth_user is None:
                        send("530 5.7.0 Authentication required")
                        continue
                    m = re.match(r"MAIL FROM:\s*<([^>]*)>", line, re.I)
                    mail_from = m.group(1) if m else ""
                    send("250 2.1.0 Ok")
                elif up.startswith("RCPT TO:"):
                    if fake.rcpt_code:
                        send(f"{fake.rcpt_code} {fake.rcpt_code // 100}.1.1 Recipient rejected")
                        continue
                    m = re.match(r"RCPT TO:\s*<([^>]*)>", line, re.I)
                    rcpts.append(m.group(1) if m else "")
                    send("250 2.1.5 Ok")
                elif up == "DATA":
                    if not rcpts:
                        send("554 5.5.1 No valid recipients")
                        continue
                    send("354 End data with <CR><LF>.<CR><LF>")
                    chunks: list[bytes] = []
                    while True:
                        ln = f.readline()
                        if not ln:
                            return
                        if ln in (b".\r\n", b".\n"):
                            break
                        if ln.startswith(b".."):
                            ln = ln[1:]
                        chunks.append(ln)
                    data = b"".join(chunks)
                    with fake.lock:
                        fake.messages.append({
                            "mail_from": mail_from, "rcpt_to": list(rcpts),
                            "data": data, "auth_user": auth_user, "tls": tls,
                        })
                    send("250 2.0.0 Ok: queued")
                    mail_from, rcpts = None, []
                elif up == "RSET":
                    mail_from, rcpts = None, []
                    send("250 2.0.0 Ok")
                elif up == "NOOP":
                    send("250 2.0.0 Ok")
                elif up == "QUIT":
                    send("221 2.0.0 Bye")
                    return
                else:
                    send("502 5.5.2 Command not recognized")
        except (OSError, ssl.SSLError):
            return


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeSMTPServer:
    """``with FakeSMTPServer(...) as srv: ... srv.port, srv.messages``."""

    def __init__(self, *, mode: str = "plain", tls_files: Optional[dict] = None,
                 auth: Optional[tuple[str, str]] = None,
                 fail_greeting_times: int = 0, rcpt_code: int = 0,
                 offer_starttls: bool = True):
        assert mode in ("plain", "starttls", "ssl")
        self.mode = mode
        self.auth = auth
        self.fail_greeting_times = fail_greeting_times
        self.rcpt_code = rcpt_code
        self.offer_starttls = offer_starttls
        self.messages: list[dict] = []
        self.commands: list[str] = []
        self.connections = 0
        self.lock = threading.Lock()
        self.server_ctx: Optional[ssl.SSLContext] = None
        if mode in ("starttls", "ssl"):
            assert tls_files, "TLS modes need make_tls_material()"
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls_files["cert"], tls_files["key"])
            self.server_ctx = ctx
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.fake = self  # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        daemon=True)

    def __enter__(self) -> "FakeSMTPServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def parsed(self, i: int = -1):
        return message_from_bytes(self.messages[i]["data"], policy=policy.default)


def closed_port() -> int:
    """A localhost port with nothing listening (connection refused)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
