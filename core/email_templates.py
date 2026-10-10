"""Transactional email templates (EMAIL E1).

Two mails, each as a plain-text body plus a simple HTML alternative:

- email verification: ``{PUBLIC_BASE_URL}/verify/{token}``
  (SvelteKit route ``web/src/routes/verify/[token]``)
- password reset: ``{PUBLIC_BASE_URL}/reset?token={token}``
  (SvelteKit route ``web/src/routes/reset``, posts to
  ``/auth/forgot/consume``)

The token is placed in the link only. Callers must never log the
rendered bodies or the link. Every value interpolated into the HTML
part is escaped.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import quote

PRODUCT_NAME = "Video Factory"

# Must match the expiries set in api/auth.py.
VERIFY_TTL_TEXT = "24 hours"
RESET_TTL_TEXT = "1 hour"


@dataclass(frozen=True)
class RenderedEmail:
    kind: str
    subject: str
    text: str
    html: str


def verify_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/verify/{quote(token, safe='')}"


def reset_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/reset?token={quote(token, safe='')}"


def _html_page(*, heading: str, intro: str, link: str, button: str,
               footer: str) -> str:
    h = html.escape
    href = h(link, quote=True)
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width\">"
        f"<title>{h(heading)}</title></head>\n"
        "<body style=\"margin:0;padding:24px;background:#f6f6f4;"
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "color:#1b1b1b;\">\n"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" "
        "cellspacing=\"0\"><tr><td align=\"center\">\n"
        "<table role=\"presentation\" width=\"480\" cellpadding=\"0\" "
        "cellspacing=\"0\" style=\"max-width:480px;background:#ffffff;"
        "border:1px solid #e3e3df;border-radius:8px;padding:28px;\">\n"
        f"<tr><td><h1 style=\"margin:0 0 12px;font-size:20px;\">{h(heading)}</h1>\n"
        f"<p style=\"margin:0 0 20px;line-height:1.5;\">{h(intro)}</p>\n"
        f"<p style=\"margin:0 0 20px;\"><a href=\"{href}\" "
        "style=\"display:inline-block;padding:10px 18px;background:#1b1b1b;"
        "color:#ffffff;text-decoration:none;border-radius:6px;\">"
        f"{h(button)}</a></p>\n"
        "<p style=\"margin:0 0 8px;font-size:13px;color:#555;\">"
        "If the button does not work, copy this link into your browser:</p>\n"
        f"<p style=\"margin:0 0 20px;font-size:13px;word-break:break-all;\">"
        f"<a href=\"{href}\" style=\"color:#1b1b1b;\">{h(link)}</a></p>\n"
        f"<p style=\"margin:0;font-size:13px;color:#555;line-height:1.5;\">"
        f"{h(footer)}</p>\n"
        "</td></tr></table>\n"
        "</td></tr></table>\n"
        "</body></html>\n"
    )


def render_verification(*, link: str) -> RenderedEmail:
    subject = f"Verify your email for {PRODUCT_NAME}"
    intro = (f"Confirm your email address for your {PRODUCT_NAME} account "
             "by opening the link below.")
    footer = (f"The link expires in {VERIFY_TTL_TEXT}. If you did not create "
              f"a {PRODUCT_NAME} account, ignore this email.")
    text = (
        "Hi,\n\n"
        f"{intro}\n\n"
        f"{link}\n\n"
        f"{footer}\n\n"
        f"- {PRODUCT_NAME}\n"
    )
    return RenderedEmail(
        kind="verify", subject=subject, text=text,
        html=_html_page(heading="Verify your email", intro=intro,
                        link=link, button="Verify email", footer=footer),
    )


def render_password_reset(*, link: str) -> RenderedEmail:
    subject = f"Reset your {PRODUCT_NAME} password"
    intro = ("Someone asked to reset the password of your "
             f"{PRODUCT_NAME} account. Open the link below to choose a "
             "new password.")
    footer = (f"The link expires in {RESET_TTL_TEXT} and works once. If you "
              "did not ask for a reset, ignore this email: your password "
              "stays the same.")
    text = (
        "Hi,\n\n"
        f"{intro}\n\n"
        f"{link}\n\n"
        f"{footer}\n\n"
        f"- {PRODUCT_NAME}\n"
    )
    return RenderedEmail(
        kind="reset", subject=subject, text=text,
        html=_html_page(heading="Reset your password", intro=intro,
                        link=link, button="Choose a new password",
                        footer=footer),
    )
