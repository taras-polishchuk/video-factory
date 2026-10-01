<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  let companies = $state([]);
  let company = $derived(companies.find(c => c.company_id === $page.data.companyId) || companies[0]);
  let bibles = $state([]);
  let active = $state(null);
  let form = $state({
    name: '', audience: '', positioning: '', tone: 'neutral',
    primary: '#0d9488', background: '#0f172a', text: '#ffffff', accent: '#f59e0b',
    cta: '', approved_script: '', prohibited: ''
  });
  let saveError = $state('');
  let saving = $state(false);

  async function load() {
    companies = await api.listCompanies();
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

<h1>Company Video Bible</h1>
<p style="color: var(--text-dim); margin-top: 0;">Versioned brand + production rules. Each approved version is locked in for jobs that reference it.</p>

{#if !company}
  <div class="card">Select a workspace to manage its Bible.</div>
{:else}
  <div class="card">
    <h2>{company.name}</h2>
    <div style="color: var(--text-dim); margin-bottom: 16px;">{company.description || 'No description.'}</div>

    {#if bibles.length === 0}
      <p style="color: var(--text-dim);">No Bible yet. Fill the form below and save a draft.</p>
    {:else}
      <table>
        <thead><tr><th>Version</th><th>Status</th><th>Created</th><th>Approved</th><th></th></tr></thead>
        <tbody>
          {#each bibles as b}
            <tr style:background={active?.bible_id === b.bible_id ? 'var(--bg-elev-2)' : ''}>
              <td>v{b.version}</td>
              <td><span class="pill {b.status === 'approved' ? 'ok' : 'muted'}">{b.status}</span></td>
              <td style="color: var(--text-dim);">{b.created_at}</td>
              <td style="color: var(--text-dim);">{b.approved_at || '—'}</td>
              <td>
                <button class="ghost" onclick={() => active = b}>View</button>
                {#if b.status === 'draft'}
                  <button onclick={() => approve(b)}>Approve</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <div class="card">
    <h2>{active ? `v${active.version} (${active.status})` : 'New draft'}</h2>

    {#if saveError}
      <div class="card" style="border-color: var(--err); color: var(--err);">{saveError}</div>
    {/if}

    <div class="row">
      <div class="col"><label>Brand name</label><input bind:value={form.name} placeholder={company.name} /></div>
      <div class="col"><label>Audience</label><input bind:value={form.audience} placeholder="home baristas 25-45" /></div>
    </div>

    <div class="row">
      <div class="col"><label>Positioning</label><input bind:value={form.positioning} /></div>
      <div class="col"><label>Tone</label><input bind:value={form.tone} /></div>
    </div>

    <div class="row">
      <div class="col"><label>CTA</label><input bind:value={form.cta} /></div>
      <div class="col"><label>Approved script (optional)</label><input bind:value={form.approved_script} /></div>
    </div>

    <label style="margin-top: 12px;">Prohibited phrases / imagery (one per line)</label>
    <textarea bind:value={form.prohibited} rows="3"></textarea>

    <h2 style="margin-top: 24px;">Brand colors</h2>
    <div class="row">
      <div class="col"><label>Primary</label><input bind:value={form.primary} /></div>
      <div class="col"><label>Background</label><input bind:value={form.background} /></div>
      <div class="col"><label>Text</label><input bind:value={form.text} /></div>
      <div class="col"><label>Accent</label><input bind:value={form.accent} /></div>
    </div>

    <div style="margin-top: 24px; display: flex; gap: 12px; align-items: center;">
      <button onclick={saveDraft} disabled={saving || !form.audience}>
        {saving ? 'Saving…' : 'Save draft'}
      </button>
      <span style="color: var(--text-dim); font-size: 12px;">
        Save creates a new draft. Approve to lock it for jobs.
      </span>
    </div>
  </div>
{/if}