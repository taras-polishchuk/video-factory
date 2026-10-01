<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';

  let companies = $state([]);
  let company = $derived(companies.find(c => c.company_id === localStorage.getItem("vf.company_id")) || companies[0]);
  let bibles = $state([]);
  let active = $state(null);
  let form = $state({
    name: '', audience: '', positioning: '', tone: 'neutral',
    primary: '#6ee86e', background: '#151515', text: '#f5f5f5', accent: '#ffd84d',
    cta: '', approved_script: '', prohibited: ''
  });
  let saveError = $state('');
  let saving = $state(false);
  let loaded = $state(false);

  async function load() {
    companies = await api.listCompanies();
    // Promote first id to idem fallback
    if (companies.length && !localStorage.getItem("vf.company_id")) {
      const id = companies[0].company_id;
      localStorage.setItem('vf.company_id', id);
      if (typeof localStorage !== 'undefined') localStorage.setItem('vf.company_id', id);
    }
    loaded = true;
  }

  $effect(() => {
    if (company) {
      api.listBibles(company.company_id).then(bs => {
        bibles = bs;
        active = bs[0] || null;
      });
    }
  });

  onMount(load);

  async function saveDraft() {
    if (!company) return;
    saveError = '';
    saving = true;
    try {
      const payload = {
        name: form.name || company.name,
        audience: form.audience,
        positioning: form.positioning,
        tone: form.tone,
        colors: {
          primary: form.primary,
          background: form.background,
          text: form.text,
          accent: form.accent,
        },
        cta: form.cta,
        approved_script: form.approved_script,
        prohibited: form.prohibited.split('\n').map(s => s.trim()).filter(Boolean),
      };
      const b = await api.createBible(company.company_id, payload);
      bibles = [b, ...bibles];
      active = b;
    } catch (e) { saveError = e.message; }
    finally { saving = false; }
  }

  async function approve(bible) {
    const updated = await api.approveBible(bible.bible_id);
    bibles = bibles.map(b => b.bible_id === updated.bible_id ? updated : b);
    if (active?.bible_id === updated.bible_id) active = updated;
  }
</script>

<svelte:head><title>Video Bible · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">Brand</p>
      <h1>Video Bible</h1>
      <p class="sub">Versioned brand + production rules. Every approved version is locked in for jobs that reference it; older drafts stay visible for diffing.</p>
    </div>
    {#if company}
      <div class="header-meta">
        <span class="vf-pill muted"><span class="dot"></span>{company.name}</span>
      </div>
    {/if}
  </div>
</header>

{#if !company}
  <div class="vf-card empty">
    <p>Select a workspace to manage its Bible.</p>
  </div>
{:else}
  <section class="vf-card">
    <header class="card-header">
      <div>
        <h2>Versions</h2>
        <p class="muted">{bibles.length} version{bibles.length === 1 ? '' : 's'} on record.</p>
      </div>
    </header>

    {#if !loaded}
      <p class="muted">Loading…</p>
    {:else if bibles.length === 0}
      <div class="empty">
        <p>No Bible yet.</p>
        <p class="muted">Fill the form below and save a draft. Approve to lock it for jobs.</p>
      </div>
    {:else}
      <div class="version-grid">
        {#each bibles as b}
          <button class="version hoverable" data-active={b.bible_id === active?.bible_id} onclick={() => active = b}>
            <div class="version-head">
              <span class="version-num">v{b.version}</span>
              <span class="vf-pill {b.status === 'approved' ? 'ok' : 'muted'}"><span class="dot"></span>{b.status}</span>
            </div>
            <div class="version-meta">
              <span class="muted">{b.created_at}</span>
              {#if b.approved_at}<span class="muted">approved {b.approved_at}</span>{/if}
            </div>
            {#if b.bible_id === active?.bible_id}
              <div class="version-active-bar"></div>
            {/if}
          </button>
        {/each}
      </div>
    {/if}
  </section>

  <section class="vf-card vf-card-glow">
    <header class="card-header">
      <div>
        <h2>{active ? `Editing v${active.version} (${active.status})` : 'New draft'}</h2>
        <p class="muted">Save creates a new draft. Approve to lock it for jobs.</p>
      </div>
    </header>

    {#if saveError}
      <div class="alert error">{saveError}</div>
    {/if}

    <div class="vf-row">
      <div class="vf-col" style="flex: 2 1 320px;">
        <label>Brand name</label>
        <input bind:value={form.name} placeholder={company.name} />
      </div>
      <div class="vf-col">
        <label>Audience</label>
        <input bind:value={form.audience} placeholder="home baristas 25-45" />
      </div>
    </div>

    <div class="vf-row">
      <div class="vf-col">
        <label>Positioning</label>
        <input bind:value={form.positioning} />
      </div>
      <div class="vf-col">
        <label>Tone</label>
        <input bind:value={form.tone} />
      </div>
    </div>

    <div class="vf-row">
      <div class="vf-col">
        <label>CTA</label>
        <input bind:value={form.cta} />
      </div>
      <div class="vf-col">
        <label>Approved script (optional)</label>
        <input bind:value={form.approved_script} />
      </div>
    </div>

    <label>Prohibited phrases / imagery (one per line)</label>
    <textarea bind:value={form.prohibited} rows="3" placeholder="No medical claims&#10;No competitor logos"></textarea>

    <h3 class="section-h3">Brand colors</h3>
    <div class="color-grid">
      <div class="color-cell">
        <input type="color" bind:value={form.primary} aria-label="primary color" />
        <div>
          <label>Primary</label>
          <input type="text" bind:value={form.primary} class="hex" />
        </div>
      </div>
      <div class="color-cell">
        <input type="color" bind:value={form.background} aria-label="background color" />
        <div>
          <label>Background</label>
          <input type="text" bind:value={form.background} class="hex" />
        </div>
      </div>
      <div class="color-cell">
        <input type="color" bind:value={form.text} aria-label="text color" />
        <div>
          <label>Text</label>
          <input type="text" bind:value={form.text} class="hex" />
        </div>
      </div>
      <div class="color-cell">
        <input type="color" bind:value={form.accent} aria-label="accent color" />
        <div>
          <label>Accent</label>
          <input type="text" bind:value={form.accent} class="hex" />
        </div>
      </div>
    </div>

    <div class="actions">
      <button class="vf-btn vf-btn-primary" onclick={saveDraft} disabled={saving || !form.audience}>
        {saving ? 'Saving…' : active ? 'Save as new draft' : 'Save draft'}
      </button>
      {#if active && active.status === 'draft'}
        <button class="vf-btn" onclick={() => approve(active)}>Approve v{active.version}</button>
      {/if}
    </div>
  </section>
{/if}

<style>
  .page-header { margin-bottom: var(--vf-sp-7); }
  .header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: var(--vf-sp-5);
    flex-wrap: wrap;
  }
  .eyebrow {
    font-size: var(--vf-fs-cap-sm);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--vf-text-secondary);
    margin: 0 0 var(--vf-sp-2);
  }
  .sub { color: var(--vf-text-secondary); max-width: 640px; margin-top: var(--vf-sp-3); }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--vf-sp-3);
    margin-bottom: var(--vf-sp-5);
  }
  .muted { color: var(--vf-text-secondary); }

  .empty {
    text-align: center;
    padding: var(--vf-sp-7);
    color: var(--vf-text-secondary);
  }

  .version-grid {
    display: grid;
    gap: var(--vf-sp-3);
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  }
  .version {
    position: relative;
    text-align: left;
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    padding: var(--vf-sp-4) var(--vf-sp-4) var(--vf-sp-4) calc(var(--vf-sp-4) + 8px);
    cursor: pointer;
    color: inherit;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--vf-sp-3);
    transition:
      transform var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
    overflow: hidden;
  }
  .version.hoverable:hover { transform: translateY(-1px); border-color: var(--vf-border-default); }
  .version[data-active="true"] {
    border-color: var(--vf-butter-green);
    background: var(--vf-surface-3);
  }
  .version-active-bar {
    position: absolute;
    left: 0;
    top: var(--vf-sp-3);
    bottom: var(--vf-sp-3);
    width: 3px;
    background: var(--vf-butter-green);
    border-radius: 0 var(--vf-radius-pill) var(--vf-radius-pill) 0;
  }
  .version-head {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--vf-sp-2);
    margin-bottom: var(--vf-sp-2);
  }
  .version-num {
    font-family: var(--vf-font-mono);
    font-weight: var(--vf-fw-medium);
    font-size: var(--vf-fs-panels);
    color: var(--vf-text-primary);
  }
  .version-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: var(--vf-fs-cap);
  }

  .section-h3 {
    font-size: var(--vf-fs-titles);
    margin: var(--vf-sp-7) 0 var(--vf-sp-4);
    color: var(--vf-text-primary);
  }

  .color-grid {
    display: grid;
    gap: var(--vf-sp-3);
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  }
  .color-cell {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-3);
    padding: var(--vf-sp-3);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
  }
  .color-cell input[type='color'] {
    width: 40px;
    height: 40px;
    padding: 0;
    border: 1px solid var(--vf-border-default);
    border-radius: var(--vf-radius-md);
    cursor: pointer;
  }
  .color-cell > div { flex: 1; min-width: 0; }
  .color-cell label { margin: 0 0 2px; }
  .color-cell .hex {
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-cap);
    text-transform: uppercase;
    padding: 6px 8px;
  }

  .alert.error {
    background: var(--vf-error-surface);
    color: var(--vf-error-text);
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: var(--vf-radius-md);
    padding: var(--vf-sp-3);
    margin-bottom: var(--vf-sp-4);
    font-size: var(--vf-fs-ui);
  }

  .actions {
    display: flex;
    gap: var(--vf-sp-3);
    margin-top: var(--vf-sp-6);
    flex-wrap: wrap;
  }
</style>