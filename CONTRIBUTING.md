# Contributing

## Local setup

```bash
git clone https://github.com/taras-polishchuk/video-factory.git
cd video-factory
python3 -m pip install --user 'jsonschema>=4.0' pytest \
                            'fastapi>=0.115' 'uvicorn>=0.30' \
                            'python-multipart>=0.0.9'
PYTHONPATH=. python3 -m pytest tests/ -q
cd web && npm install
```

## Code conventions

### Identity discipline

Every commit is signed by the same person who runs `git config`. The
identity-guard pre-commit hook verifies the commit email matches the
authenticated GitHub account. See `~/.gitconfig`.

### Python

- TypeScript-first doesn't apply; Python is the primary backend.
- `pyright` types are expected on new files.
- New modules register a test in `tests/`.
- Mock-mode default. Live paths require a separate, verified smoke
  test (see `docs/ROADMAP.md` items 4-7).

### JavaScript / Svelte

- TypeScript-only for `*.ts` and `.svelte`. Svelte 5 runes (`$state`,
  `$derived`, `$effect`, `$props`).
- Avoid `any`; use Zod schemas at API boundaries if you need parsing.
- Each route in `web/src/routes/` keeps a single page.

## Git workflow

- `main` is the protected branch. All changes go through a PR.
- One topic per PR. PR title mirrors commit message subject.
- CI must pass before merge: `pytest tests/`, `npm run check`,
  `npm run build`.

## Adding a render provider

1. Create `render_plane/<provider>.py`.
2. Conform to the `Renderer` protocol in `render_plane/provider.py`.
3. Wire into `render_plane/registry.py:default_registry()`.
4. Add a provider-matrix row in `docs/provider-matrix.md`.
5. Add a contract test in `tests/test_provider_contracts.py`.
6. Verify by running `LIVE_PROVIDER_TESTS=true MAX_COST_PER_BATCH_USD=5
   PYTHONPATH=. python3 -m pytest tests/`. The new adapter should
   raise `ProviderDisabled` until env vars are configured for live
   use; this is the gate the audit requires.

## Adding a pipeline stage

1. Add the stage key to `core/runners.py:STAGE_SEQUENCE`.
2. Implement the stage body inside `_run()`.
3. Record start / finish via `_stage_started()` /
   `_stage_done()`.
4. Add a UI line in `web/src/routes/jobs/[id]/+page.svelte` (the
   timeline iterates `stages` and renders them dynamically; usually
   no edit is needed).

## Adding a public API endpoint

1. Add the route inside `api/main.py:create_app()`.
2. Always scope by `company_id`. Cross-company access raises
   `Forbidden` from `core/repo.py`.
3. Test in `tests/test_api.py`. Cross-company tests are required.
4. Update `docs/agent-integration.md` with the endpoint, request
   shape, and sample response.

## What not to commit

- `artifacts/` (state directory; gitignored).
- `.env` or any file containing real secrets.
- `node_modules/`, `__pycache__/`, `.svelte-kit/`.
- Generated files like `*.db`, `*.mp4`.

The `.gitignore` at the repo root enforces most of this; double-check
before pushing.