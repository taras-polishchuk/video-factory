"""UI U1 — regression pins for the broken customer path in `web/`.

Source findings: AUDIT A3 (artifacts/t_3669c18d-audit-e2e.md).

  A-1/A-4  layout-level EmptyState replaced every page until a provider
           key existed (the credentials form itself was unreachable)
  A-2      POST /login -> 500 "When using named actions, the default
           action cannot be used"
  A-5      <label> / control association on every form
  A-6      no navigation at <= 880px
  A-7      topbar email clipped
  A-9      "Create + approve one" link on /create
  A-10     <video> without a captions track
  A-11/12  deprecated on:click, state captured once from `form`
  A-13     /favicon.svg 404
  A-14     "Add provider key" copy

Plus the defects found while walking the path in a real browser:
  - the browser API client never sent X-CSRF-Token (bible approve 403)
  - asset upload passed (kind, file) in the wrong order
  - the workspace switcher posted to /login?/switch and ended on the
    login page

Most checks are static (the SvelteKit app is source files; no TS runtime
in the pytest sandbox), a few run real code under node (api-client with a
mocked fetch, the redirect guard) and one runs svelte-check. The live
browser walk is in tests/test_u1_ui_browser.py.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
ROUTES = WEB_ROOT / "src" / "routes"
LIB = WEB_ROOT / "src" / "lib"

NODE = shutil.which("node")


# ──────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────


def _blank_comments_and_strings(src: str) -> str:
    """Return `src` with comments removed and string/template literal
    bodies replaced by underscores (quotes kept), so brace matching and
    key scanning cannot be fooled by `{` inside text or `//` in a URL."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if c in "\"'`":
            quote = c
            out.append(quote)
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\":
                    i += 1
                out.append("_")
                i += 1
            out.append(quote)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def action_keys(src: str) -> list[str]:
    """Top-level keys of `export const actions = { ... }` in a
    +page.server.ts. Raises AssertionError on entries it cannot read, so
    a refactor can never make the pin pass silently."""
    clean = _blank_comments_and_strings(src)
    m = re.search(r"export\s+const\s+actions\b[^={]*=\s*\{", clean)
    if not m:
        return []
    depth = 1
    top: list[str] = []
    for ch in clean[m.end():]:
        if ch in "{([":
            depth += 1
            if depth == 2:
                top.append("\x00")  # entry value placeholder
            continue
        if ch in "})]":
            depth -= 1
            if depth == 0:
                break
            continue
        if depth == 1:
            top.append(ch)
    entries = [e for e in "".join(top).split(",") if e.strip().strip("\x00")]
    keys = []
    for e in entries:
        km = re.match(r"\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*(?::|\x00)", e)
        assert km, f"cannot parse action entry {e!r}"
        keys.append(km.group(1))
    return keys


def server_files() -> list[pathlib.Path]:
    files = sorted(ROUTES.rglob("+page.server.ts")) + sorted(ROUTES.rglob("+page.server.js"))
    assert files, "no +page.server.* files found: path layout changed?"
    return files


def svelte_files() -> list[pathlib.Path]:
    files = sorted(ROUTES.rglob("*.svelte")) + sorted(LIB.rglob("*.svelte"))
    assert files
    return files


def rel(p: pathlib.Path) -> str:
    return str(p.relative_to(REPO_ROOT))


# ──────────────────────────────────────────────────────────────────
# A-2: default + named action mix
# ──────────────────────────────────────────────────────────────────


def test_action_key_parser_negative_control():
    """The detector must actually detect: feed it the exact shape that
    broke /login and a few look-alikes."""
    broken = """
    export const actions: Actions = {
        default: async ({ request }) => { return { a: { b: 1 } }; },
        switch: switch_workspace_action,
    };
    """
    assert action_keys(broken) == ["default", "switch"]
    fixed = """
    // default: not an action, just a comment
    export const actions: Actions = {
        login: async ({ request }) => { const u = "http://x:1/{y}"; return u; },
        switch: switch_workspace_action,
    };
    """
    assert action_keys(fixed) == ["login", "switch"]
    only_default = "export const actions: Actions = { default: async () => {} };"
    assert action_keys(only_default) == ["default"]
    assert action_keys("export const load = () => ({});") == []


def test_no_route_mixes_default_and_named_actions():
    """A-2 regression pin. SvelteKit throws 'When using named actions,
    the default action cannot be used' on POST (HTTP 500) for a route
    that exports `default` next to any named action."""
    offenders = {}
    for f in server_files():
        keys = action_keys(f.read_text())
        if "default" in keys and len(keys) > 1:
            offenders[rel(f)] = keys
    assert not offenders, f"default + named actions on one route: {offenders}"


def test_login_route_actions_are_named_login_and_switch():
    keys = action_keys((ROUTES / "login" / "+page.server.ts").read_text())
    assert sorted(keys) == ["login", "switch"]


def test_every_post_form_targets_an_action_that_exists():
    """For each route with form actions: a <form method=POST> with no
    `action=` needs a `default` action; `?/name` must name a real one.
    Also checks cross-route `/route?/name` form actions (the workspace
    switcher posts to /login?/switch)."""
    problems = []
    form_tag = re.compile(r"<form\b(?:[^>{]|\{[^}]*\})*>", re.S)

    for server in server_files():
        keys = action_keys(server.read_text())
        page = server.with_name("+page.svelte")
        if not keys or not page.exists():
            continue
        for tag in form_tag.findall(page.read_text()):
            if not re.search(r"method\s*=\s*[\"']?post", tag, re.I):
                continue
            am = re.search(r"action\s*=\s*(\{`?|[\"'])([^\"'`}]*)", tag)
            if not am:
                if "default" not in keys:
                    problems.append(f"{rel(page)}: POST form without action= but route has only {keys}")
                continue
            target = am.group(2)
            nm = re.match(r"\?/(\w+)", target)
            if nm and nm.group(1) not in keys:
                problems.append(f"{rel(page)}: form targets ?/{nm.group(1)}, route has {keys}")

    # cross-route references like action={`/login?/switch&back=...`}
    for f in svelte_files():
        for m in re.finditer(r"""action\s*=\s*\{?[`"'](/[\w/\[\]-]*)\?/(\w+)""", f.read_text()):
            route, name = m.group(1), m.group(2)
            server = ROUTES / route.strip("/") / "+page.server.ts"
            if not server.exists():
                problems.append(f"{rel(f)}: posts to {route} which has no +page.server.ts")
            elif name not in action_keys(server.read_text()):
                problems.append(f"{rel(f)}: posts to {route}?/{name}, route has {action_keys(server.read_text())}")
    assert not problems, "\n".join(problems)


def test_login_form_keeps_next_across_a_failed_attempt():
    """The re-rendered /login?/login URL has no ?next=, so the form
    action must carry it, otherwise one typo drops the redirect."""
    page = (ROUTES / "login" / "+page.svelte").read_text()
    assert re.search(r"action=\{`\?/login&next=\$\{encodeURIComponent\(data\.next\)\}`\}", page)
    assert 'name="next"' in page


# ──────────────────────────────────────────────────────────────────
# A-1/A-4: layout hijack
# ──────────────────────────────────────────────────────────────────


def test_layout_always_renders_children():
    layout = (ROUTES / "+layout.svelte").read_text()
    assert layout.count("{@render children()}") == 1
    for banned in ("EmptyState", "firstCredentialExists", "showEmpty"):
        assert banned not in layout, f"+layout.svelte must not contain {banned!r} (A-1/A-4)"
    # the single render of children is not inside a conditional branch
    main = layout[layout.index("<main"):layout.index("</main>")]
    assert "{#if" not in main and "{:else" not in main


def test_onboarding_empty_state_only_on_overview():
    users = [rel(f) for f in svelte_files()
             if f.name != "EmptyState.svelte" and re.search(r"import\s+EmptyState\b", f.read_text())]
    assert users == ["web/src/routes/+page.svelte"], users
    overview = (ROUTES / "+page.svelte").read_text()
    assert "firstCredentialExists === false" in overview
    assert 'ctaHref="/credentials"' in overview
    # A-14: a first-time user is told what a provider key is and that
    # mock mode needs none.
    assert "A provider key is" in overview
    assert "Mock renders need no key" in overview


def test_layout_loader_still_exposes_first_credential_flag():
    server = (ROUTES / "+layout.server.ts").read_text()
    assert "firstCredentialExists" in server


# ──────────────────────────────────────────────────────────────────
# A-6 / A-7: mobile navigation + topbar
# ──────────────────────────────────────────────────────────────────


def test_mobile_navigation_is_an_accessible_disclosure_menu():
    layout = (ROUTES / "+layout.svelte").read_text()
    assert 'aria-expanded={menuOpen}' in layout
    ctrl = re.search(r'aria-controls="([\w-]+)"', layout)
    assert ctrl, "menu button needs aria-controls"
    assert f'id="{ctrl.group(1)}"' in layout, "aria-controls must point at the panel"
    assert "'Escape'" in layout or '"Escape"' in layout
    assert "afterNavigate" in layout, "menu must close after navigation"
    assert 'aria-label="Primary"' in layout
    assert "aria-current" in layout
    # the audit's anti-pattern: nav hidden with nothing to replace it
    style = layout[layout.index("<style>"):]
    assert not re.search(r"\.topnav\s*\{\s*display:\s*none", style)
    # the toggle must be shown at phone widths
    assert re.search(r"@media \(max-width: (\d+)px\)[^@]*\.menu-toggle\s*\{[^}]*display:\s*inline", style, re.S)
    assert max(int(x) for x in re.findall(r"@media \(max-width: (\d+)px\)", style)) >= 880


def test_topbar_email_truncates_with_ellipsis():
    style = (ROUTES / "+layout.svelte").read_text()
    pill = re.search(r"\.user-pill\s*\{([^}]*)\}", style, re.S).group(1)
    for needle in ("max-width", "text-overflow: ellipsis", "overflow: hidden", "white-space: nowrap"):
        assert needle in pill, needle


# ──────────────────────────────────────────────────────────────────
# A-5: label / control association
# ──────────────────────────────────────────────────────────────────

_CONTROL = re.compile(r"<(input|select|textarea)\b((?:[^>{]|\{[^}]*\})*)>", re.S)
_LABEL = re.compile(r"<label\b((?:[^>{]|\{[^}]*\})*)>(.*?)</label>", re.S)


def _norm(v: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", v.strip())


@pytest.mark.parametrize("path", svelte_files(), ids=rel)
def test_every_form_control_has_an_accessible_name(path):
    src = path.read_text()
    labels = [(m.start(), m.end(), m.group(1), m.group(2)) for m in _LABEL.finditer(src)]
    for_values = {_norm(v) for _, _, attrs, _ in labels for v in re.findall(r"\bfor=[\"']([^\"']+)[\"']", attrs)}
    bad = []
    for m in _CONTROL.finditer(src):
        attrs = m.group(2)
        if re.search(r"type=[\"'](hidden|submit|button)[\"']", attrs):
            continue
        if re.search(r"\b(aria-label|aria-labelledby)=", attrs):
            continue
        id_m = re.search(r"\bid=[\"']([^\"']+)[\"']", attrs)
        if id_m and _norm(id_m.group(1)) in for_values:
            continue
        if any(s < m.start() < e for s, e, _, _ in labels):  # wrapped by a <label>
            continue
        bad.append(m.group(0)[:80].replace("\n", " "))
    assert not bad, f"{rel(path)}: controls without an accessible name: {bad}"


@pytest.mark.parametrize("path", svelte_files(), ids=rel)
def test_every_label_has_a_control(path):
    src = path.read_text()
    ids = {_norm(v) for v in re.findall(r"\bid=[\"']([^\"']+)[\"']", src)}
    bad = []
    for m in _LABEL.finditer(src):
        attrs, inner = m.group(1), m.group(2)
        f = re.search(r"\bfor=[\"']([^\"']+)[\"']", attrs)
        if f:
            if _norm(f.group(1)) not in ids:
                bad.append(f"for={f.group(1)} has no matching id")
        elif not _CONTROL.search(inner):
            bad.append(f"<label> without for= and without a nested control: {inner.strip()[:40]!r}")
    assert not bad, f"{rel(path)}: {bad}"


# ──────────────────────────────────────────────────────────────────
# A-9 / A-10 / A-11 / A-12 / A-13
# ──────────────────────────────────────────────────────────────────


def test_create_page_links_to_bible_editor():
    """A-9: the 'create + approve one' hint is a real, visibly styled
    link (global `a` has no underline), with a draft-aware variant."""
    page = (ROUTES / "create" / "+page.svelte").read_text()
    assert re.search(r'<a href="/bible" class="link">[^<]*Create \+ approve one', page)
    assert re.search(r'<a href="/bible" class="link">[^<]*approve one', page)
    css = re.search(r"\.link\s*\{([^}]*)\}", page).group(1)
    assert "underline" in css


def test_every_video_has_a_captions_track():
    seen = 0
    for f in svelte_files():
        src = f.read_text()
        for m in re.finditer(r"<video\b.*?</video>", src, re.S):
            seen += 1
            assert re.search(r'<track\s+kind="captions"', m.group(0)), rel(f)
    assert seen >= 1


def test_static_assets_exist_and_are_referenced():
    fav = WEB_ROOT / "static" / "favicon.svg"
    assert fav.exists() and fav.read_text().lstrip().startswith("<svg")
    assert "favicon.svg" in (WEB_ROOT / "src" / "app.html").read_text()
    vtt = WEB_ROOT / "static" / "captions-none.vtt"
    assert vtt.read_text().startswith("WEBVTT")
    assert "/captions-none.vtt" in (ROUTES / "jobs" / "[id]" / "+page.svelte").read_text()


def test_no_deprecated_event_directives():
    offenders = [rel(f) for f in svelte_files() if re.search(r"\bon:[a-z]+\s*=", f.read_text())]
    assert not offenders, offenders


def test_auth_pages_do_not_capture_form_state_once_unguarded():
    """A-12: `let email = $state(form?.email || "")` captured only the
    first value (Svelte state_referenced_locally). The intentional
    one-shot seed must go through untrack()."""
    for name in ("login", "signup", "forgot"):
        src = (ROUTES / name / "+page.svelte").read_text()
        assert not re.search(r"\$state\((?!untrack)[^)]*\bform\b", src), name
        assert "untrack" in src, name


# ──────────────────────────────────────────────────────────────────
# loading / error / empty states
# ──────────────────────────────────────────────────────────────────


def test_error_page_exists_and_never_echoes_5xx_detail():
    err = (ROUTES / "+error.svelte").read_text()
    assert "role=\"alert\"" in err
    assert "status >= 500" in err
    # raw `$page.error.message` only for non-5xx
    assert re.search(r"status >= 500\s*\?[^:]*:\s*\(\$page\.error", err, re.S)


@pytest.mark.parametrize(
    "rel_path",
    ["+page.svelte", "jobs/+page.svelte", "bible/+page.svelte", "create/+page.svelte",
     "assets/+page.svelte", "credentials/+page.svelte", "integrations/+page.svelte",
     "jobs/[id]/+page.svelte"],
)
def test_data_pages_have_loading_and_error_states(rel_path):
    src = (ROUTES / rel_path).read_text()
    assert "StatusNote" in src, rel_path
    # create/ shows loading inside the Bible <select> ("Loading Bibles…")
    assert 'kind="loading"' in src or re.search(r"Phase === 'loading'", src), rel_path
    assert 'kind="error"' in src or "role=\"alert\"" in src, rel_path
    assert "describeError" in src, rel_path
    # no more bare "Loading…" paragraphs that never resolve on failure
    assert not re.search(r'<p class="muted">Loading', src), rel_path


# ──────────────────────────────────────────────────────────────────
# svelte-check
# ──────────────────────────────────────────────────────────────────


def test_svelte_check_reports_zero_errors_and_zero_warnings():
    if not (WEB_ROOT / "node_modules" / ".bin" / "svelte-check").exists():
        pytest.skip("svelte-check not installed (no node_modules)")
    proc = subprocess.run(
        ["npx", "--no-install", "svelte-check", "--tsconfig", "./jsconfig.json", "--output", "machine"],
        cwd=str(WEB_ROOT), capture_output=True, text=True, timeout=240,
    )
    out = proc.stdout + proc.stderr
    m = re.search(r"COMPLETED \d+ FILES (\d+) ERRORS (\d+) WARNINGS", out)
    assert m, out[-1500:]
    errors, warnings = int(m.group(1)), int(m.group(2))
    problems = [ln for ln in out.splitlines() if " WARNING " in ln or " ERROR " in ln]
    assert (errors, warnings) == (0, 0), "\n".join(problems[:20])


# ──────────────────────────────────────────────────────────────────
# Real code under node: api-client (CSRF, upload args) + redirect guard
# ──────────────────────────────────────────────────────────────────

needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _node(script: str) -> dict:
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, cwd=str(WEB_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_api_client_sends_csrf_on_mutations_only_and_caches_token():
    result = _node(f"""
    import {{ api }} from {json.dumps((LIB / "api-client.js").as_uri())};
    const calls = [];
    const json = (b, status = 200) => new Response(JSON.stringify(b), {{ status, headers: {{ 'content-type': 'application/json' }} }});
    globalThis.fetch = async (url, init = {{}}) => {{
      const headers = init.headers || {{}};
      calls.push({{ url, method: init.method || 'GET', csrf: headers['X-CSRF-Token'] || null }});
      if (url === '/auth/csrf') return json({{ csrf_token: 'tok-1' }});
      if (url.endsWith('/approve')) {{
        return headers['X-CSRF-Token'] === 'tok-1' ? json({{ bible_id: 'b1', status: 'approved' }}) : json({{ error: 'invalid CSRF token' }}, 403);
      }}
      return json([]);
    }};
    await api.listBibles('w1');
    await api.approveBible('b1');
    await api.approveBible('b2');
    console.log(JSON.stringify(calls));
    """)
    assert result[0] == {"url": "/api/v1/workspaces/w1/bibles", "method": "GET", "csrf": None}
    assert [c["url"] for c in result].count("/auth/csrf") == 1, "token must be fetched once and cached"
    posts = [c for c in result if c["method"] == "POST"]
    assert len(posts) == 2 and all(c["csrf"] == "tok-1" for c in posts)


@needs_node
def test_api_client_refreshes_a_stale_csrf_token_once():
    result = _node(f"""
    import {{ api }} from {json.dumps((LIB / "api-client.js").as_uri())};
    let issued = 0; const seen = [];
    const json = (b, status = 200) => new Response(JSON.stringify(b), {{ status, headers: {{ 'content-type': 'application/json' }} }});
    globalThis.fetch = async (url, init = {{}}) => {{
      const h = init.headers || {{}};
      if (url === '/auth/csrf') {{ issued += 1; return json({{ csrf_token: issued === 1 ? 'stale' : 'fresh' }}); }}
      seen.push(h['X-CSRF-Token']);
      return h['X-CSRF-Token'] === 'fresh' ? json({{ ok: true }}) : json({{ error: 'invalid CSRF token' }}, 403);
    }};
    const r = await api.putCredential('w1', 'heygen_direct', 'k');
    console.log(JSON.stringify({{ r, seen, issued }}));
    """)
    assert result == {"r": {"ok": True}, "seen": ["stale", "fresh"], "issued": 2}


@needs_node
def test_api_client_does_not_loop_on_a_real_403():
    """A genuine permission 403 (not CSRF) is surfaced, not retried."""
    result = _node(f"""
    import {{ api, describeError }} from {json.dumps((LIB / "api-client.js").as_uri())};
    let posts = 0;
    const json = (b, status = 200) => new Response(JSON.stringify(b), {{ status, headers: {{ 'content-type': 'application/json' }} }});
    globalThis.fetch = async (url, init = {{}}) => {{
      if (url === '/auth/csrf') return json({{ csrf_token: 't' }});
      posts += 1;
      return json({{ error: 'requires role >= editor' }}, 403);
    }};
    let msg = null, status = null;
    try {{ await api.approveBible('b'); }} catch (e) {{ msg = describeError(e); status = e.status; }}
    console.log(JSON.stringify({{ posts, msg, status }}));
    """)
    assert result == {"posts": 1, "msg": "requires role >= editor", "status": 403}


@needs_node
def test_api_client_still_sends_when_token_lookup_fails():
    result = _node(f"""
    import {{ api }} from {json.dumps((LIB / "api-client.js").as_uri())};
    const sent = [];
    globalThis.fetch = async (url, init = {{}}) => {{
      if (url === '/auth/csrf') return new Response('nope', {{ status: 404 }});
      sent.push({{ url, hasHeader: !!(init.headers || {{}})['X-CSRF-Token'] }});
      return new Response('{{}}', {{ status: 200, headers: {{ 'content-type': 'application/json' }} }});
    }};
    await api.createJob({{ topic: 't' }});
    console.log(JSON.stringify(sent));
    """)
    assert result == [{"url": "/api/v1/jobs", "hasHeader": False}]


@needs_node
def test_upload_asset_sends_the_file_not_the_kind():
    """Regression: assets/+page.svelte called uploadAsset(wid, 'asset',
    file) while the helper is (wid, file, kind): the multipart body
    carried the string 'asset' and the kind became '[object File]'."""
    page = (ROUTES / "assets" / "+page.svelte").read_text()
    assert re.search(r"uploadAsset\(company\.workspace_id,\s*file,\s*'asset'\)", page)
    result = _node(f"""
    import {{ api }} from {json.dumps((LIB / "api-client.js").as_uri())};
    let seen = null;
    globalThis.fetch = async (url, init = {{}}) => {{
      if (url === '/auth/csrf') return new Response(JSON.stringify({{ csrf_token: 'x' }}), {{ status: 200, headers: {{ 'content-type': 'application/json' }} }});
      const f = init.body.get('file');
      seen = {{ url, isFile: f instanceof File, name: f.name, ct: (init.headers || {{}})['Content-Type'] || null }};
      return new Response('{{}}', {{ status: 200, headers: {{ 'content-type': 'application/json' }} }});
    }};
    await api.uploadAsset('w1', new File(['abc'], 'logo.png', {{ type: 'image/png' }}), 'asset');
    console.log(JSON.stringify(seen));
    """)
    assert result == {"url": "/api/v1/workspaces/w1/assets?kind=asset", "isFile": True, "name": "logo.png", "ct": None}


@needs_node
@pytest.mark.parametrize(
    "case,expected",
    [
        ("{ status: 401, message: 'x' }", "Your session has expired. Log in again to continue."),
        ("{ status: 404, message: 'x' }", "That item was not found."),
        ("{ status: 500, message: 'Traceback (most recent call last)' }", "The server hit a problem. Try again in a moment."),
        ("{ status: 403, message: '{\"x\":1}' }", "You don't have access to this workspace."),
        ("new TypeError('Failed to fetch')", "Can't reach the server. Check your connection and try again."),
        ("new Error('<html>boom</html>')", "Something went wrong. Try again."),
        ("new Error('Pick a smaller file')", "Pick a smaller file"),
    ],
)
def test_describe_error_never_shows_raw_blobs(case, expected):
    result = _node(f"""
    import {{ describeError }} from {json.dumps((LIB / "api-client.js").as_uri())};
    console.log(JSON.stringify(describeError({case})));
    """)
    assert result == expected


@needs_node
@pytest.mark.parametrize(
    "target,expected",
    [
        ("/jobs", "/jobs"),
        ("/jobs?state=done#top", "/jobs?state=done#top"),
        ("/jobs/job_1/../job_2", "/jobs/job_2"),
        ("//evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("/\t/evil.example", "/"),
        ("/\n/evil.example", "/"),
        ("\\\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_same_origin_path_guard(target, expected):
    # node >= 22.6 strips the TS annotations natively; older -> skip
    ver = subprocess.run([NODE, "-p", "process.versions.node"], capture_output=True, text=True).stdout.strip()
    if tuple(int(x) for x in ver.split(".")[:2]) < (22, 18):
        pytest.skip(f"node {ver} cannot import .ts without a flag")
    src = (LIB / "server" / "safe-redirect.ts").as_uri()
    result = _node(f"""
    import {{ same_origin_path }} from {json.dumps(src)};
    console.log(JSON.stringify(same_origin_path({json.dumps(target)})));
    """)
    assert result == expected


def test_login_action_uses_the_redirect_guard():
    src = (ROUTES / "login" / "+page.server.ts").read_text()
    assert "same_origin_path(next)" in src
    # the long-standing pins from tests/test_wave3_ui.py stay true
    assert 'next.startsWith("/")' in src and '!next.startsWith("//")' in src


# ──────────────────────────────────────────────────────────────────
# workspace switcher + dev proxy
# ──────────────────────────────────────────────────────────────────


def test_switcher_action_sends_csrf_and_redirects_back():
    helper = (LIB / "server" / "auth-cookies.ts").read_text()
    body = helper[helper.index("export async function switch_workspace_action"):]
    assert "Auth.switch_workspace" in body, "must use the wrapper that adds X-CSRF-Token"
    assert "api_request(" not in body
    assert "redirect(303" in body and "same_origin_path(" in body
    switcher = (LIB / "components" / "WorkspaceSwitcher.svelte").read_text()
    assert "back=${encodeURIComponent(" in switcher


def test_dev_proxy_forwards_auth_routes_like_production_caddy():
    """The browser client fetches /auth/csrf. Caddy sends /auth/* to the
    API in production; vite dev must do the same."""
    vite = (WEB_ROOT / "vite.config.js").read_text()
    assert "'/auth'" in vite
    caddy = (REPO_ROOT / "Caddyfile").read_text()
    assert "/auth/*" in caddy
