<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';

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

  let company = $derived(null);
  let items = $state([]);
  let editing = $state({});   // provider -> { value: '', saving: false, error: '' }
  let flash = $state({ kind: '', message: '' });

  onMount(async () => {
    // The layout passes the active company down via localStorage +
    // /api/v1/companies lookup. We re-resolve here for simplicity.
    try {
      const cs = await api.listCompanies();
      const saved = typeof localStorage !== 'undefined' && localStorage.getItem('vf.company_id');
      const found = saved && cs.find(c => c.company_id === saved);
      company = (found || cs[0] || null);
      if (company) await reload();
    } catch (e) {
      flash = { kind: 'error', message: e.message };
    }
  });

  async function reload() {
    if (!company) return;
    try {
      items = await api.listCredentials(company.company_id);
    } catch (e) {
      flash = { kind: 'error', message: e.message };
    }
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
      const operator = (typeof localStorage !== 'undefined' && localStorage.getItem('vf.operator')) || 'operator';
      await api.putCredential(company.company_id, p, v, operator);
      cancelEditor(p);
      await reload();
      flash = { kind: 'ok', message: `${p} saved (fingerprint will update)` };
    } catch (e) {
      editing = { ...editing, [p]: { ...editing[p], saving: false, error: e.message } };
    }
  }

  async function deleteCredential(p) {
    if (!confirm(`Delete the saved credential for ${p}? This cannot be undone.`)) return;
    try {
      const operator = (typeof localStorage !== 'undefined' && localStorage.getItem('vf.operator')) || 'operator';
      await api.deleteCredential(company.company_id, p, operator);
      await reload();
      flash = { kind: 'ok', message: `${p} deleted` };
    } catch (e) {
      flash = { kind: 'error', message: e.message };
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
  <div class="flash {flash.kind}">{flash.message}</div>
{/if}

<section class="vf-card">
  {#if !company}
    <p class="muted">Select a workspace to manage credentials.</p>
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
                <input
                  type="password"
                  autocomplete="off"
                  placeholder="paste plaintext secret"
                  bind:value={editing[p.id].value}
                  disabled={editing[p.id].saving}
                />
                <button
                  class="primary"
                  on:click={() => saveCredential(p.id)}
                  disabled={editing[p.id].saving}
                >
                  {editing[p.id].saving ? 'saving…' : 'save'}
                </button>
                <button class="ghost" on:click={() => cancelEditor(p.id)} disabled={editing[p.id].saving}>
                  cancel
                </button>
                {#if editing[p.id].error}
                  <p class="error">{editing[p.id].error}</p>
                {/if}
              </div>
            {:else if rec}
              <button class="ghost" on:click={() => openEditor(p.id)}>rotate</button>
              <button class="danger" on:click={() => deleteCredential(p.id)}>delete</button>
            {:else}
              <button class="primary" on:click={() => openEditor(p.id)}>add secret</button>
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