<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';

  let companies = $state([]);
  let company = $derived(companies[0]);
  let bibles = $state([]);
  let form = $state({
    topic: '', language: 'en-US', duration_seconds: 30,
    platforms: 'tiktok,reels', aspect_ratio: '9:16', count: 1,
    bible_id: ''
  });
  let err = $state('');
  let submitting = $state(false);

  $effect(() => {
    if (company) api.listBibles(company.company_id).then(bs => {
      bibles = bs.filter(b => b.status === 'approved');
      if (bibles[0]) form.bible_id = bibles[0].bible_id;
    });
  });

  onMount(async () => { companies = await api.listCompanies(); });

  async function submit() {
    if (!company || !form.bible_id) {
      err = 'Pick an approved Bible version.';
      return;
    }
    err = ''; submitting = true;
    try {
      const j = await api.createJob({
        company_id: company.company_id,
        bible_id: form.bible_id,
        topic: form.topic,
        language: form.language,
        duration_seconds: Number(form.duration_seconds),
        platforms: form.platforms.split(',').map(s => s.trim()).filter(Boolean),
        aspect_ratio: form.aspect_ratio,
        count: Number(form.count),
        render_profile: 'mock',
      });
      goto('/jobs/' + j.job_id);
    } catch (e) { err = e.message; }
    finally { submitting = false; }
  }
</script>

<svelte:head><title>Create video · Video Factory</title></svelte:head>

<h1>Create video</h1>
<p style="color: var(--text-dim); margin-top: 0;">Mock mode by default. No paid provider calls. Set a brief and confirm.</p>

<div class="card">
  {#if err}<div style="color: var(--err); margin-bottom: 12px;">{err}</div>{/if}

  <div class="row">
    <div class="col" style="flex: 2 1 320px;">
      <label>Topic / brief</label>
      <input bind:value={form.topic} placeholder="Why altitude changes your morning coffee" />
    </div>
    <div class="col">
      <label>Language</label>
      <select bind:value={form.language}>
        <option>en-US</option><option>en-GB</option><option>uk-UA</option>
        <option>de-DE</option><option>es-ES</option><option>fr-FR</option>
      </select>
    </div>
  </div>

  <div class="row">
    <div class="col"><label>Duration (seconds)</label><input type="number" bind:value={form.duration_seconds} min="3" max="120" /></div>
    <div class="col"><label>Aspect ratio</label>
      <select bind:value={form.aspect_ratio}>
        <option>9:16</option><option>16:9</option><option>1:1</option><option>4:5</option>
      </select>
    </div>
    <div class="col"><label>Number of variants</label><input type="number" bind:value={form.count} min="1" max="10" /></div>
  </div>

  <label>Target platforms (comma-separated)</label>
  <input bind:value={form.platforms} placeholder="tiktok, reels, shorts" />

  <label style="margin-top: 12px;">Brand Bible version (must be approved)</label>
  <select bind:value={form.bible_id}>
    {#each bibles as b}<option value={b.bible_id}>{company?.name} v{b.version} ({b.status})</option>{/each}
  </select>
  {#if bibles.length === 0}
    <p style="color: var(--warn); margin-top: 8px;">No approved Bible yet. <a href="/bible" style="color: var(--accent);">Create + approve one →</a></p>
  {/if}

  <div style="margin-top: 24px;">
    <button onclick={submit} disabled={submitting || !company || !form.bible_id}>
      {submitting ? 'Submitting…' : 'Submit mock job'}
    </button>
  </div>
</div>