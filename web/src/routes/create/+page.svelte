<script>
  import { api, describeError } from '$lib/api-client.js';
  import StatusNote from '$lib/components/StatusNote.svelte';
  import { goto } from '$app/navigation';
  import { untrack } from 'svelte';

  let { data } = $props();

  // Active workspace comes from the layout's server-loaded data.
  let company = $derived(data.activeWorkspace || null);
  let bibles = $state([]);       // approved only (selectable)
  let draftCount = $state(0);    // unapproved ones, for the empty hint
  let biblesPhase = $state('loading'); // loading | ready | error
  let biblesError = $state('');
  let form = $state({
    topic: '', language: 'en-US', duration_seconds: 30,
    platforms: 'tiktok,reels', aspect_ratio: '9:16', count: 1,
    bible_id: '', notes: ''
  });
  let err = $state('');
  let submitting = $state(false);
  let submittingState = $state('idle'); // idle | submitting | queued | done

  async function loadBibles() {
    if (!company) return;
    biblesPhase = 'loading';
    biblesError = '';
    try {
      const all = await api.listBibles(company.workspace_id);
      bibles = all.filter(b => b.status === 'approved');
      draftCount = all.length - bibles.length;
      if (bibles[0] && !form.bible_id) form.bible_id = bibles[0].bible_id;
      biblesPhase = 'ready';
    } catch (e) {
      biblesError = describeError(e);
      biblesPhase = 'error';
    }
  }

  // Re-run only when the active workspace changes, not on every
  // keystroke / selection inside the form.
  $effect(() => {
    if (company) untrack(loadBibles);
  });

  async function submit() {
    if (!company || !form.bible_id) {
      err = 'Pick an approved Bible version first.';
      return;
    }
    err = '';
    submitting = true;
    submittingState = 'submitting';
    try {
      const j = await api.createJob({
        company_id: company.workspace_id,
        bible_id: form.bible_id,
        topic: form.topic,
        language: form.language,
        duration_seconds: Number(form.duration_seconds),
        platforms: form.platforms.split(',').map(s => s.trim()).filter(Boolean),
        aspect_ratio: form.aspect_ratio,
        count: Number(form.count),
        render_profile: 'mock',
      });
      submittingState = 'queued';
      setTimeout(() => goto('/jobs/' + j.job_id), 350);
    } catch (e) { err = describeError(e); submittingState = 'idle'; }
    finally { submitting = false; }
  }
</script>

<svelte:head><title>Create video · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">New job</p>
      <h1>Create a video</h1>
      <p class="sub">Mock-mode by default. No paid provider calls, no GPU, no API keys. Set a brief, choose a Bible version, submit.</p>
    </div>
  </div>
</header>

{#if !company}
  <div class="vf-card empty">
    <p>Pick a workspace first.</p>
  </div>
{:else}
  <form class="vf-card vf-card-glow" onsubmit={(e) => { e.preventDefault(); submit(); }}>
    {#if err}<div class="alert error" role="alert">{err}</div>{/if}

    <div class="vf-row">
      <div class="vf-col" style="flex: 2 1 320px;">
        <label for="create-topic">Topic / brief</label>
        <input id="create-topic" name="topic" bind:value={form.topic} placeholder="Why altitude changes your morning coffee" required />
      </div>
      <div class="vf-col">
        <label for="create-language">Language</label>
        <select id="create-language" name="language" bind:value={form.language}>
          <option value="en-US">English (US)</option>
          <option value="en-GB">English (UK)</option>
          <option value="uk-UA">Ukrainian</option>
          <option value="de-DE">German</option>
          <option value="es-ES">Spanish</option>
          <option value="fr-FR">French</option>
        </select>
      </div>
    </div>

    <div class="vf-row">
      <div class="vf-col">
        <label for="create-duration">Duration (seconds)</label>
        <input id="create-duration" name="duration_seconds" type="number" bind:value={form.duration_seconds} min="3" max="120" />
      </div>
      <div class="vf-col">
        <label for="create-aspect">Aspect ratio</label>
        <select id="create-aspect" name="aspect_ratio" bind:value={form.aspect_ratio}>
          <option value="9:16">9:16 (vertical)</option>
          <option value="16:9">16:9 (horizontal)</option>
          <option value="1:1">1:1 (square)</option>
          <option value="4:5">4:5 (portrait)</option>
        </select>
      </div>
      <div class="vf-col">
        <label for="create-count">Number of variants</label>
        <input id="create-count" name="count" type="number" bind:value={form.count} min="1" max="10" />
      </div>
    </div>

    <label for="create-platforms">Target platforms (comma-separated)</label>
    <input id="create-platforms" name="platforms" bind:value={form.platforms} placeholder="tiktok, reels, shorts" />

    <label for="create-bible">Brand Bible version (must be approved)</label>
    <select id="create-bible" name="bible_id" bind:value={form.bible_id} disabled={biblesPhase === 'loading'}>
      <option value="">{biblesPhase === 'loading' ? 'Loading Bibles…' : '— pick one —'}</option>
      {#each bibles as b}
        <option value={b.bible_id}>{company.name} v{b.version} · {b.status}</option>
      {/each}
    </select>
    {#if biblesPhase === 'error'}
      <StatusNote kind="error" title="Couldn’t load your Bibles" message={biblesError} onretry={loadBibles} />
    {:else if biblesPhase === 'ready' && bibles.length === 0}
      <p class="muted hint" data-testid="no-approved-bible">
        {#if draftCount > 0}
          You have {draftCount} draft Bible{draftCount === 1 ? '' : 's'} but none is approved yet.
          <a href="/bible" class="link">Open the Video Bible and approve one →</a>
        {:else}
          No approved Bible yet.
          <a href="/bible" class="link">Create + approve one →</a>
        {/if}
      </p>
    {/if}

    <div class="actions">
      <button type="submit" class="vf-btn vf-btn-primary vf-btn-lg" disabled={submitting || !company || !form.bible_id || !form.topic}>
        {#if submittingState === 'submitting'}
          <span class="spin"></span> Submitting…
        {:else if submittingState === 'queued'}
          ✓ Queued
        {:else}
          Submit mock job
        {/if}
      </button>
      <p class="muted actions-note">
        Mock mode renders locally with FFmpeg. Outputs land in the job’s storage bucket and survive a restart.
      </p>
    </div>
  </form>
{/if}

<style>
  .page-header { margin-bottom: var(--vf-sp-7); }
  .header-row { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--vf-sp-5); flex-wrap: wrap; }
  .eyebrow {
    font-size: var(--vf-fs-cap-sm);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--vf-text-secondary);
    margin: 0 0 var(--vf-sp-2);
  }
  .sub { color: var(--vf-text-secondary); max-width: 640px; margin-top: var(--vf-sp-3); }
  .muted { color: var(--vf-text-secondary); }
  .link { color: var(--vf-butter-green); text-decoration: underline; text-underline-offset: 3px; }

  .empty { text-align: center; padding: var(--vf-sp-7); color: var(--vf-text-secondary); }

  .alert.error {
    background: var(--vf-error-surface);
    color: var(--vf-error-text);
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: var(--vf-radius-md);
    padding: var(--vf-sp-3);
    margin-bottom: var(--vf-sp-5);
    font-size: var(--vf-fs-ui);
  }

  form .vf-row { margin-bottom: var(--vf-sp-2); }
  form label + input,
  form label + select {
    margin-bottom: var(--vf-sp-4);
  }
  form label { margin-top: var(--vf-sp-2); }

  .hint { font-size: var(--vf-fs-cap); margin: -8px 0 var(--vf-sp-4); }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-4);
    margin-top: var(--vf-sp-7);
    padding-top: var(--vf-sp-5);
    border-top: 1px solid var(--vf-border-subtle);
    flex-wrap: wrap;
  }
  .actions-note { margin: 0; flex: 1; font-size: var(--vf-fs-cap); }

  .spin {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid currentColor;
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>