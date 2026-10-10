<script>
  import { api, describeError } from '$lib/api-client.js';
  import StatusNote from '$lib/components/StatusNote.svelte';
  import { untrack } from 'svelte';

  let { data } = $props();

  // Provider catalog. The set mirrors repo.CREDENTIAL_PROVIDERS on the
  // server side. Adding a new provider here must be paired with that
  // set, otherwise the API returns 400.
  const PROVIDERS = [
    { id: 'heygen_direct',         label: 'HeyGen (direct REST)',     env: 'HEYGEN_API_KEY' },
    { id: 'comfy_cloud',           label: 'ComfyUI Cloud',            env: 'COMFY_CLOUD_API_KEY' },
    { id: 'comfy_gpu_worker',      label: 'ComfyUI GPU worker (VPS)', env: 'COMFY_GPU_API_BASE' },
    { id: 'comfy_heygen_partner',  label: 'ComfyUI HeyGen Partner',   env: 'COMFY_GPU_API_BASE' },
    { id: 'publer',                label: 'Publer (publishing)',      env: 'PUBLER_API_KEY' },
  ];

  // Active workspace comes from the layout's server-loaded data.
  // No client-side storage — the cookie identifies the user; the layout
  // resolves the active workspace from the session.
  let company = $derived(data.activeWorkspace || null);
  let items = $state([]);
  let phase = $state('loading'); // loading | ready | error
  let loadError = $state('');
  let editing = $state({});   // provider -> { value: '', saving: false, error: '' }
  let flash = $state({ kind: '', message: '' });

  $effect(() => {
    if (company) untrack(reload);
  });

  async function reload() {
    if (!company) return;
    try {
      items = await api.listCredentials(company.workspace_id);
      phase = 'ready';
    } catch (e) {
      loadError = describeError(e);
      // Keep what is already on screen after a failed refresh; only
      // the first load turns into the error state.
      if (phase === 'loading') phase = 'error';
      else flash = { kind: 'error', message: loadError };
    }
  }

  function retry() {
    phase = 'loading';
    return reload();
  }

  function providerRecord(p) {
    return items.find(i => i.provider === p);
  }

  function openEditor(p) {
    editing = { ...editing, [p]: { value: '', saving: false, error: '' } };
  }

  function cancelEditor(p) {
    const copy = { ...editing };
    delete copy[p];
    editing = copy;
  }

  async function saveCredential(p) {
    const v = (editing[p]?.value || '').trim();
    if (!v) {
      editing = { ...editing, [p]: { ...editing[p], error: 'value required' } };
      return;
    }
    editing = { ...editing, [p]: { ...editing[p], saving: true, error: '' } };
    try {
      await api.putCredential(company.workspace_id, p, v);
      cancelEditor(p);
      await reload();
      flash = { kind: 'ok', message: `${p} saved (fingerprint will update)` };
    } catch (e) {
      editing = { ...editing, [p]: { ...editing[p], saving: false, error: describeError(e) } };
    }
  }

  async function deleteCredential(p) {
    if (!confirm(`Delete the saved credential for ${p}? This cannot be undone.`)) return;
    try {
      await api.deleteCredential(company.workspace_id, p);
      await reload();
      flash = { kind: 'ok', message: `${p} deleted` };
    } catch (e) {
      flash = { kind: 'error', message: describeError(e) };
    }
  }

  function fmtDate(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
    } catch { return iso; }
  }
</script>

<svelte:head><title>Credentials · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">Provider credentials</p>
      <h1>API keys & tokens</h1>
      <p class="sub">
        Each company keeps its own encrypted credentials. The server encrypts
        on save with the platform master key (<code>VIDEO_FACTORY_SECRET_KEY</code>);
        plaintext is never stored, logged, or echoed back through the API.
      </p>
    </div>
    {#if company}
      <div class="company-chip" title={company.slug}>{company.name}</div>
    {/if}
  </div>
</header>

{#if flash.message}
  <div class="flash {flash.kind}" role={flash.kind === 'error' ? 'alert' : 'status'}>{flash.message}</div>
{/if}

<section class="vf-card">
  {#if !company}
    <p class="muted">Select a workspace to manage credentials.</p>
  {:else if phase === 'loading'}
    <StatusNote kind="loading" message="Loading saved credentials…" />
  {:else if phase === 'error'}
    <StatusNote kind="error" title="Couldn’t load credentials" message={loadError} onretry={retry} />
  {:else}
    <div class="provider-list">
      {#each PROVIDERS as p}
        {@const rec = providerRecord(p.id)}
        <article class="provider-row" class:has-value={!!rec}>
          <div class="provider-meta">
            <h3>{p.label}</h3>
            <code class="env-name">env · {p.env}</code>
            {#if rec}
              <p class="muted small">
                fingerprint · <code>{rec.fingerprint}</code> · updated {fmtDate(rec.updated_at)} · by {rec.updated_by}
              </p>
            {:else}
              <p class="muted small">no credential saved</p>
            {/if}
          </div>

          <div class="provider-actions">
            {#if editing[p.id]}
              <div class="editor">
                <label class="sr-only" for="cred-{p.id}">Secret for {p.label}</label>
                <input
                  id="cred-{p.id}"
                  name="secret-{p.id}"
                  type="password"
                  autocomplete="off"
                  placeholder="paste plaintext secret"
                  bind:value={editing[p.id].value}
                  disabled={editing[p.id].saving}
                  aria-invalid={!!editing[p.id].error}
                  aria-describedby={editing[p.id].error ? `cred-${p.id}-err` : undefined}
                />
                <button
                  type="button"
                  class="primary"
                  onclick={() => saveCredential(p.id)}
                  disabled={editing[p.id].saving}
                >
                  {editing[p.id].saving ? 'saving…' : 'save'}
                </button>
                <button type="button" class="ghost" onclick={() => cancelEditor(p.id)} disabled={editing[p.id].saving}>
                  cancel
                </button>
                {#if editing[p.id].error}
                  <p class="error" id="cred-{p.id}-err" role="alert">{editing[p.id].error}</p>
                {/if}
              </div>
            {:else if rec}
              <button type="button" class="ghost" onclick={() => openEditor(p.id)}>rotate</button>
              <button type="button" class="danger" onclick={() => deleteCredential(p.id)}>delete</button>
            {:else}
              <button type="button" class="primary" onclick={() => openEditor(p.id)}>add secret</button>
            {/if}
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>

<section class="vf-card vf-card-glow">
  <h2>How it works</h2>
  <ul class="explain">
    <li>
      <strong>Per-company isolation.</strong> Each workspace's keys are stored
      in a separate row scoped to that workspace's ID. A request for another
      workspace's key returns 404.
    </li>
    <li>
      <strong>Server-side encryption.</strong> The browser sends the plaintext
      over the API; the API encrypts with Fernet before writing. The DB never
      sees plaintext. Only a SHA-256 fingerprint is surfaced for display.
    </li>
    <li>
      <strong>Render-plane handoff.</strong> When a job submits, the render
      adapter decrypts the credential in-process and passes it to the
      upstream API. Plaintext is never persisted to logs or audit records.
    </li>
    <li>
      <strong>Audit log.</strong> Every write and delete lands in
      <code>runner_audit</code> with the operator name and fingerprint, so
      rotation history is visible without exposing the value.
    </li>
    <li>
      <strong>Platform master key.</strong> <code>VIDEO_FACTORY_SECRET_KEY</code>
      must be set in the API server env. Without it, the server refuses to
      accept writes. Rotating it requires re-entering every credential.
    </li>
  </ul>
</section>

<style>
  .page-header { margin-bottom: var(--vf-sp-7); }
  .header-row {
    display: flex; justify-content: space-between; align-items: flex-end;
    gap: var(--vf-sp-5); flex-wrap: wrap;
  }
  .eyebrow {
    font-size: var(--vf-fs-cap-sm); text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--vf-text-secondary);
    margin: 0 0 var(--vf-sp-2);
  }
  .sub { color: var(--vf-text-secondary); max-width: 640px; margin-top: var(--vf-sp-3); }
  .muted { color: var(--vf-text-secondary); }
  .small { font-size: var(--vf-fs-cap); margin: var(--vf-sp-1) 0 0; }
  .company-chip {
    font-size: var(--vf-fs-cap); color: var(--vf-text-secondary);
    padding: 4px 12px; border-radius: var(--vf-radius-pill);
    background: var(--vf-surface-1); border: 1px solid var(--vf-border);
  }

  .flash {
    padding: var(--vf-sp-3) var(--vf-sp-4); margin-bottom: var(--vf-sp-4);
    border-radius: var(--vf-radius-md); font-size: var(--vf-fs-ui);
    border: 1px solid var(--vf-border);
  }
  .flash.ok { background: var(--vf-surface-1); color: var(--vf-text-primary); border-color: rgba(110, 232, 110, 0.35); }
  .flash.error { background: var(--vf-error-surface); color: var(--vf-error-text); border-color: rgba(248, 113, 113, 0.25); }

  .provider-list { display: flex; flex-direction: column; }
  .provider-row {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: var(--vf-sp-5); flex-wrap: wrap;
    padding: var(--vf-sp-4) 0;
    border-bottom: 1px solid var(--vf-border-subtle);
  }
  .provider-row:last-child { border-bottom: 0; }
  .provider-meta { min-width: 0; flex: 1 1 260px; }
  .provider-meta h3 { margin: 0 0 var(--vf-sp-1); }
  .env-name { font-size: var(--vf-fs-cap-sm); }
  .provider-actions { display: flex; gap: var(--vf-sp-2); align-items: center; flex-wrap: wrap; }
  .editor { display: flex; gap: var(--vf-sp-2); align-items: center; flex-wrap: wrap; flex: 1 1 320px; }
  .editor input { flex: 1 1 200px; min-width: 0; width: auto; }
  .editor .error { flex-basis: 100%; margin: 0; color: var(--vf-error-text); font-size: var(--vf-fs-cap); }

  .provider-actions button, .editor button {
    padding: 8px 14px; border-radius: var(--vf-radius-md);
    border: 1px solid var(--vf-border); background: var(--vf-surface-2);
    color: var(--vf-text-primary); font: inherit; font-size: var(--vf-fs-ui);
    cursor: pointer; min-height: 36px;
  }
  .provider-actions button:disabled, .editor button:disabled { opacity: 0.6; cursor: not-allowed; }
  button.primary { background: var(--vf-butter-green); color: #0a0909; border-color: transparent; font-weight: var(--vf-fw-semibold); }
  button.ghost { background: transparent; }
  button.danger { background: transparent; color: var(--vf-error-text); border-color: rgba(248, 113, 113, 0.35); }

  .explain { margin: 0; padding-left: var(--vf-sp-5); display: flex; flex-direction: column; gap: var(--vf-sp-3); color: var(--vf-text-secondary); }
  .explain strong { color: var(--vf-text-primary); }

  @media (max-width: 880px) {
    .provider-actions, .editor { width: 100%; }
  }
</style>
