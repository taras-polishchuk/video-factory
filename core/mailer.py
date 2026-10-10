"""Outbound email for verification + password-reset links (EMAIL E1).

Transport selection (``Mailer.from_env``, once per app):

1. ``SMTP_HOST`` set and the SMTP config is valid -> ``smtp``
   (stdlib ``smtplib``; STARTTLS, implicit TLS or plain, see below).
2. Otherwise, when ``VIDEO_FACTORY_ENV`` is a dev/test value
   (dev, development, test, testing, local) -> ``console``: prints a
   redacted one-line notice to stderr. The full link is printed only
   with the existing local-dev opt-in ``VIDEO_FACTORY_DEV_EXPOSE_TOKENS=1``.
3. Otherwise (production, staging, unset) -> ``null``: logs
   "email not configured" and sends nothing. Never logs the token.

SMTP env:

    SMTP_HOST          relay host (unset = email not configured)
    SMTP_PORT          default 587 (starttls) / 465 (ssl) / 25 (none)
    SMTP_USER          optional; AUTH only when set
    SMTP_PASSWORD      optional; never logged, never in repr()
    SMTP_FROM          required: "no-reply@example.com" or
                       "Video Factory <no-reply@example.com>"
    SMTP_TLS           starttls (default) | ssl | none
    SMTP_TIMEOUT_SEC   socket timeout per attempt, default 10
    SMTP_CA_FILE       optional extra CA bundle (private relay)
    SMTP_MAX_ATTEMPTS  default 3
    SMTP_RETRY_BACKOFF_SEC  default 1 (waits 1 s, then 2 s)
    SMTP_QUEUE_MAX     max mails in flight, default 100

TLS is always verified (system CA store or ``SMTP_CA_FILE``); there is
no "skip verification" switch. ``starttls`` never falls back to plain
text when the server does not offer STARTTLS. ``none`` + ``SMTP_USER``
is refused in production/staging (password would cross the network in
clear text).

SMTP delivery runs on a small background thread pool, off the request
path, with retries on transient errors (network, timeouts, 4xx) and no
retry on permanent ones (5xx, auth, TLS verification). The outcome is
reported through the caller's ``audit(event, context)`` callback as
``email.sent`` / ``email.failed``. Audit context and logs carry only
the mail kind, transport, attempt count, error class and SMTP code:
never the token, the link, the body or the recipient address.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import smtplib
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Callable, Optional
from urllib.parse import urlsplit

from core.email_templates import (
    RenderedEmail,
    render_password_reset,
    render_verification,
    reset_link,
    verify_link,
)

_log = logging.getLogger("video_factory.email")

DEV_ENVS = frozenset({"dev", "development", "test", "testing", "local"})
DEPLOYED_ENVS = frozenset({"production", "prod", "staging"})

# Local dev default for links when VIDEO_FACTORY_PUBLIC_BASE_URL is
# unset (SvelteKit `vite dev`). Never used in production/staging.
DEV_PUBLIC_BASE_URL = "http://localhost:5173"

_TLS_MODES = {
    "starttls": "starttls",
    "ssl": "ssl", "tls": "ssl", "smtps": "ssl",
    "none": "none", "plain": "none", "off": "none",
}
_DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}

# Conservative ASCII address: one @, no quotes, commas, spaces, angle
# brackets or control characters, so an address can never smuggle a
# second recipient or a header line.
_ADDR_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

AuditFn = Callable[[str, dict], None]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def current_env() -> str:
    return _env("VIDEO_FACTORY_ENV").lower()


def is_valid_address(addr: str) -> bool:
    return bool(addr) and len(addr) <= 254 and bool(_ADDR_RE.match(addr))


def mask_address(addr: str) -> str:
    """``alice@example.com`` -> ``a***@example.com`` (console notice only)."""
    local, _, domain = (addr or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _dev_expose_tokens() -> bool:
    """Same opt-in as api/auth.py: explicit flag AND a dev/test env."""
    flag = _env("VIDEO_FACTORY_DEV_EXPOSE_TOKENS").lower()
    return flag in ("1", "true", "yes") and current_env() in DEV_ENVS


class MailConfigError(ValueError):
    """Invalid email configuration. ``code`` is a fixed token that is
    safe to expose on /readyz (never a value from the environment)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    sender: str            # From: header value
    envelope_from: str     # bare address for MAIL FROM
    tls: str               # starttls | ssl | none
    user: str = ""
    password: str = field(default="", repr=False)
    timeout: float = 10.0
    ca_file: str = ""

    @property
    def sender_domain(self) -> str:
        return self.envelope_from.rsplit("@", 1)[-1]


def smtp_config_from_env() -> Optional[SMTPConfig]:
    """Parse SMTP_* env. None when SMTP_HOST is unset; MailConfigError
    when it is set but the rest of the config is unusable."""
    host = _env("SMTP_HOST")
    if not host:
        return None
    if any(ch.isspace() for ch in host) or "/" in host or "@" in host:
        raise MailConfigError("invalid_smtp_host")
    tls = _TLS_MODES.get(_env("SMTP_TLS").lower() or "starttls")
    if tls is None:
        raise MailConfigError("invalid_smtp_tls")
    port_raw = _env("SMTP_PORT")
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            raise MailConfigError("invalid_smtp_port") from None
        if not 1 <= port <= 65535:
            raise MailConfigError("invalid_smtp_port")
    else:
        port = _DEFAULT_PORTS[tls]
    sender_raw = _env("SMTP_FROM")
    if not sender_raw:
        raise MailConfigError("missing_smtp_from")
    if "\r" in sender_raw or "\n" in sender_raw:
        raise MailConfigError("invalid_smtp_from")
    name, addr = parseaddr(sender_raw)
    if not is_valid_address(addr):
        raise MailConfigError("invalid_smtp_from")
    sender = formataddr((name, addr)) if name else addr
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    if password and not user:
        raise MailConfigError("smtp_password_without_user")
    timeout_raw = _env("SMTP_TIMEOUT_SEC")
    try:
        timeout = float(timeout_raw) if timeout_raw else 10.0
    except ValueError:
        raise MailConfigError("invalid_smtp_timeout") from None
    if not 0 < timeout <= 120:
        raise MailConfigError("invalid_smtp_timeout")
    ca_file = _env("SMTP_CA_FILE")
    if ca_file and not os.path.isfile(ca_file):
        raise MailConfigError("invalid_smtp_ca_file")
    if tls == "none" and user and current_env() in DEPLOYED_ENVS:
        raise MailConfigError("plaintext_auth_refused")
    return SMTPConfig(
        host=host, port=port, sender=sender, envelope_from=addr, tls=tls,
        user=user, password=password, timeout=timeout, ca_file=ca_file,
    )


def public_base_url_from_env() -> tuple[Optional[str], Optional[str]]:
    """``(base_url, error_code)`` for the links inside the mails."""
    raw = _env("VIDEO_FACTORY_PUBLIC_BASE_URL")
    if not raw:
        if current_env() in DEPLOYED_ENVS:
            return None, "missing_public_base_url"
        return DEV_PUBLIC_BASE_URL, None
    try:
        u = urlsplit(raw)
    except ValueError:
        return None, "invalid_public_base_url"
    if (u.scheme not in ("http", "https") or not u.netloc
            or u.query or u.fragment or "@" in u.netloc
            or any(ch.isspace() for ch in raw)):
        return None, "invalid_public_base_url"
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}", None


# ── transports ──────────────────────────────────────────────────


class NullTransport:
    name = "null"


class ConsoleTransport:
    """Dev/test only. One redacted line on stderr per mail."""

    name = "console"

    def notify(self, *, kind: str, to_addr: str, link: str, token: str) -> None:
        shown = link if _dev_expose_tokens() else link.replace(token, "<redacted>")
        print(
            f"[video-factory email/console] kind={kind} "
            f"to={mask_address(to_addr)} link={shown}"
            + ("" if shown == link else
               " (set VIDEO_FACTORY_DEV_EXPOSE_TOKENS=1 for the full link)"),
            file=sys.stderr, flush=True,
        )


class SMTPTransport:
    name = "smtp"

    def __init__(self, cfg: SMTPConfig):
        self.cfg = cfg

    def _ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.cfg.ca_file or None)

    def send(self, msg: EmailMessage, *, to_addr: str) -> None:
        cfg = self.cfg
        helo = cfg.sender_domain
        if cfg.tls == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, local_hostname=helo,
                timeout=cfg.timeout, context=self._ssl_context(),
            )
        else:
            client = smtplib.SMTP(cfg.host, cfg.port, local_hostname=helo,
                                  timeout=cfg.timeout)
        try:
            client.ehlo()
            if cfg.tls == "starttls":
                # Raises SMTPNotSupportedError when the server does not
                # offer STARTTLS: we never continue in plain text.
                client.starttls(context=self._ssl_context())
                client.ehlo()
            if cfg.user:
                client.login(cfg.user, cfg.password)
            client.send_message(msg, from_addr=cfg.envelope_from,
                                to_addrs=[to_addr])
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - connection already gone
                client.close()


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (smtplib.SMTPAuthenticationError,
                        smtplib.SMTPNotSupportedError,
                        ssl.SSLCertVerificationError)):
        return False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _ in (exc.recipients or {}).values()]
        return any(400 <= int(c) < 500 for c in codes)
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= int(exc.smtp_code) < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, (OSError, TimeoutError)):
        return True   # refused, reset, DNS, timeout, TLS handshake EOF
    return False


def _smtp_code(exc: BaseException) -> Optional[int]:
    if isinstance(exc, smtplib.SMTPResponseException):
        return int(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        for code, _ in (exc.recipients or {}).values():
            return int(code)
    return None


# ── background pool (shared by every Mailer in the process) ─────

_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="vf-mailer",
            )
        return _POOL


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_env(name) or default)
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(_env(name) or default)
    except ValueError:
        return default
    return max(lo, min(hi, v))


class Mailer:
    """Builds and dispatches the two transactional mails. Every public
    method is best-effort and never raises into the request."""

    def __init__(self, *, transport, public_base_url: Optional[str],
                 error: Optional[str] = None, max_attempts: int = 3,
                 backoff_sec: float = 1.0, queue_max: int = 100,
                 sleep: Callable[[float], None] = time.sleep):
        self.transport = transport
        self.public_base_url = public_base_url
        self.error = error
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_sec = max(0.0, float(backoff_sec))
        self.queue_max = max(1, int(queue_max))
        self._sleep = sleep
        self._pending: set[concurrent.futures.Future] = set()
        self._lock = threading.Lock()

    # ── construction ──────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "Mailer":
        env = current_env()
        error: Optional[str] = None
        cfg: Optional[SMTPConfig] = None
        try:
            cfg = smtp_config_from_env()
        except MailConfigError as e:
            error = e.code
        base, base_error = public_base_url_from_env()
        if cfg is not None and base is None:
            cfg, error = None, base_error
        if cfg is not None:
            transport = SMTPTransport(cfg)
        elif env in DEV_ENVS:
            transport = ConsoleTransport()
        else:
            transport = NullTransport()
        mailer = cls(
            transport=transport, public_base_url=base, error=error,
            max_attempts=_int_env("SMTP_MAX_ATTEMPTS", 3, 1, 5),
            backoff_sec=_float_env("SMTP_RETRY_BACKOFF_SEC", 1.0, 0.0, 30.0),
            queue_max=_int_env("SMTP_QUEUE_MAX", 100, 1, 10_000),
        )
        extra = {"email_transport": transport.name}
        if error:
            extra["email_error"] = error
        if transport.name == "smtp":
            _log.info("email transport: smtp", extra=extra)
        elif error:
            _log.error("email not configured: invalid SMTP settings; "
                       "verification and reset links will not be delivered",
                       extra=extra)
        elif transport.name == "null":
            _log.warning("email not configured: verification and reset "
                         "links will not be delivered (set SMTP_HOST)",
                         extra=extra)
        return mailer

    # ── status (/readyz info) ─────────────────────────────────

    @property
    def configured(self) -> bool:
        return self.transport.name == "smtp"

    def status(self) -> dict:
        out = {
            "email": "configured" if self.configured else "not_configured",
            "email_transport": self.transport.name,
        }
        if self.error:
            out["email_error"] = self.error
        return out

    # ── public send API ───────────────────────────────────────

    def send_verification(self, *, to: str, token: str, audit: AuditFn) -> None:
        self._safe_send("verify", to, token, audit)

    def send_password_reset(self, *, to: str, token: str, audit: AuditFn) -> None:
        self._safe_send("reset", to, token, audit)

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait for in-flight deliveries (tests, graceful shutdown).
        True when nothing is left pending."""
        with self._lock:
            pending = list(self._pending)
        if not pending:
            return True
        _, not_done = concurrent.futures.wait(pending, timeout=timeout)
        return not not_done

    # ── internals ─────────────────────────────────────────────

    def _safe_send(self, kind: str, to: str, token: str, audit: AuditFn) -> None:
        try:
            self._send(kind, to, token, audit)
        except Exception as e:  # noqa: BLE001 - never break the request
            _log.error("email dispatch error",
                       extra={"email_kind": kind,
                              "error_class": type(e).__name__})

    def _audit(self, audit: AuditFn, event: str, kind: str, **ctx) -> None:
        context = {"kind": kind, "transport": self.transport.name}
        context.update({k: v for k, v in ctx.items() if v is not None})
        try:
            audit(event, context)
        except Exception as e:  # noqa: BLE001
            _log.warning("email audit failed",
                         extra={"email_kind": kind,
                                "error_class": type(e).__name__})

    def _send(self, kind: str, to: str, token: str, audit: AuditFn) -> None:
        if self.transport.name == "null":
            extra = {"email_kind": kind, "email_transport": "null"}
            if self.error:
                extra["email_error"] = self.error
            _log.warning("email not configured; %s email not sent", kind,
                         extra=extra)
            return
        to_addr = (to or "").strip()
        if not is_valid_address(to_addr):
            self._audit(audit, "email.failed", kind, attempts=0,
                        reason="invalid_recipient")
            _log.warning("email not sent: recipient address rejected",
                         extra={"email_kind": kind})
            return
        if not token or not self.public_base_url:
            self._audit(audit, "email.failed", kind, attempts=0,
                        reason="missing_link")
            return
        if kind == "verify":
            link = verify_link(self.public_base_url, token)
            rendered = render_verification(link=link)
        else:
            link = reset_link(self.public_base_url, token)
            rendered = render_password_reset(link=link)

        if self.transport.name == "console":
            self.transport.notify(kind=kind, to_addr=to_addr, link=link,
                                  token=token)
            self._audit(audit, "email.sent", kind, attempts=1)
            return

        msg = self._build_message(rendered, to_addr)
        fut: Optional[concurrent.futures.Future] = None
        with self._lock:
            if len(self._pending) < self.queue_max:
                fut = _pool().submit(self._deliver, msg, to_addr, kind, audit)
                self._pending.add(fut)
        if fut is None:
            self._audit(audit, "email.failed", kind, attempts=0,
                        reason="queue_full")
            _log.error("email not sent: delivery queue full",
                       extra={"email_kind": kind})
            return
        fut.add_done_callback(self._forget)

    def _forget(self, fut: concurrent.futures.Future) -> None:
        with self._lock:
            self._pending.discard(fut)

    def _build_message(self, rendered: RenderedEmail, to_addr: str) -> EmailMessage:
        cfg: SMTPConfig = self.transport.cfg
        msg = EmailMessage()
        msg["Subject"] = rendered.subject
        msg["From"] = cfg.sender
        msg["To"] = to_addr
        msg["Date"] = formatdate(usegmt=True)
        msg["Message-ID"] = make_msgid(domain=cfg.sender_domain)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(rendered.text)
        msg.add_alternative(rendered.html, subtype="html")
        return msg

    def _deliver(self, msg: EmailMessage, to_addr: str, kind: str,
                 audit: AuditFn) -> bool:
        last: Optional[BaseException] = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            try:
                self.transport.send(msg, to_addr=to_addr)
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_retryable(e) or attempt == self.max_attempts:
                    break
                self._sleep(self.backoff_sec * (2 ** (attempt - 1)))
                continue
            self._audit(audit, "email.sent", kind, attempts=attempt,
                        message_id=str(msg["Message-ID"]))
            _log.info("email sent", extra={"email_kind": kind,
                                           "email_transport": "smtp",
                                           "attempts": attempt})
            return True
        err = type(last).__name__ if last is not None else "unknown"
        code = _smtp_code(last) if last is not None else None
        self._audit(audit, "email.failed", kind, attempts=attempts,
                    error=err, smtp_code=code)
        _log.warning("email delivery failed",
                     extra={"email_kind": kind, "email_transport": "smtp",
                            "attempts": attempts, "error_class": err,
                            "smtp_code": code})
        return False
