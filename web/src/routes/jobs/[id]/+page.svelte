<script>
  import { api } from '$lib/api.js';
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';

  let jobId = $derived($page.params.id);
  let job = $state(null);
  let stages = $state([]);
  let outputs = $state([]);
  let publishIntents = $state([]);
  let publishResult = $state(null);
  let error = $state('');
  let loading = $state(true);
  let timer;

  async function load() {
    loading = true;
    try {
      [job, stages, outputs, publishIntents] = await Promise.all([
        api.getJob(jobId),
        api.jobStages(jobId),
        api.jobOutputs(jobId),
        api.publishList(jobId).catch(() => []),
      ]);
      error = '';
    } catch (e) {
      error = e.message;
      job = null;
    }
    loading = false;
  }

  onMount(() => {
    load();
    timer = setInterval(load, 1500);
  });
  onDestroy(() => clearInterval(timer));

  let finalVideo = $derived(outputs.find(o => o.kind === 'final_video'));
  let sceneClips = $derived(outputs.filter(o => o.kind === 'scene_clip'));

  let publishForm = $state({ action: 'draft', platforms: 'tiktok', caption: '', scheduled_for: '' });

  async function publish() {
    publishResult = null;
    const platforms = publishForm.platforms.split(',').map(s => s.trim()).filter(Boolean);
    try {
      const r = await api.publish(jobId, {
        action: publishForm.action,
        platforms,
        caption: publishForm.caption,
        scheduled_for: publishForm.scheduled_for || undefined,
      });
      publishResult = { ok: true, detail: r.detail };
      publishIntents = await api.publishList(jobId);
    } catch (e) {
      publishResult = { ok: false, detail: e.message };
    }
  }

  async function retry() {
    try {
      await api.retryJob(jobId);
      load();
    } catch (e) { error = e.message; }
  }

  function pillFor(state) {
    if (state === 'done') return 'ok';
    if (state === 'failed') return 'fail';
    if (state === 'running') return 'run';
    if (state === 'queued') return 'info';
    return 'muted';
  }

  function formatStageLabel(s) { return s.replace(/_/g, ' '); }
</script>

<svelte:head><title>{job?.topic || 'Job'} · Video Factory</title></svelte:head>

{#if error && !job}
  <div class="vf-card">
    <h1>Couldn’t load job</h1>
    <p class="muted">{error}</p>
    <a href="/jobs" class="vf-btn vf-btn-ghost">← Back to jobs</a>
  </div>
{:else if !job}
  <p class="muted">Loading…</p>
{:else}
  <header class="page-header">
    <div class="header-row">
      <div>
        <p class="eyebrow"><a href="/jobs" class="link-back">Jobs</a> · <span class="mono">{job.job_id}</span></p>
        <h1>{job.topic}</h1>
        <p class="sub">
          {job.duration_seconds}s · {job.aspect_ratio} ·
          <span class="mono">{job.platforms.join(', ')}</span>
        </p>
      </div>
      <div class="header-actions">
        <span class="vf-pill {pillFor(job.state)}"><span class="dot"></span>{job.state}</span>
        {#if job.state !== 'done'}
          <button class="vf-btn vf-btn-ghost" onclick={retry}>Retry</button>
        {/if}
      </div>
    </div>
    {#if job.status_message}
      <p class="status-msg">{job.status_message}</p>
    {/if}
  </header>

  <section class="vf-card">
    <header class="card-header">
      <div>
        <h2>Pipeline timeline</h2>
        <p class="muted">12 stages from brief through publish. Stages refresh every 1.5 s while the job is running.</p>
      </div>
    </header>

    {#if stages.length === 0}
      <p class="muted">No stages yet — the job is waiting to be picked up.</p>
    {:else}
      <ol class="timeline">
        {#each stages as s}
          <li class="stage {s.status}">
            <div class="bullet"><span class="num">{s.ordinal}</span></div>
            <div class="stage-body">
              <div class="stage-head">
                <span class="stage-name">{formatStageLabel(s.stage_key)}</span>
                <span class="vf-pill {pillFor(s.status)}"><span class="dot"></span>{s.status}</span>
                {#if s.duration_ms !== null && s.duration_ms !== undefined}
                  <span class="muted mono">{s.duration_ms} ms</span>
                {/if}
              </div>
              {#if s.error}
                <div class="stage-error">{s.error}</div>
              {/if}
              {#if s.summary && Object.keys(s.summary).length > 0}
                <details class="stage-details">
                  <summary>stage details</summary>
                  <pre>{JSON.stringify(s.summary, null, 2)}</pre>
                </details>
              {/if}
            </div>
          </li>
        {/each}
      </ol>
    {/if}
  </section>

  {#if finalVideo}
    <section class="vf-card">
      <header class="card-header">
        <div>
          <h2>Final video</h2>
          <p class="muted">{finalVideo.filename} · {Math.round(finalVideo.size_bytes / 1024)} KB · mock render</p>
        </div>
        <a href={api.outputDownloadUrl(jobId, finalVideo.output_id)} download={finalVideo.filename} class="vf-btn vf-btn-primary">Download</a>
      </header>
      <div class="player">
        <video controls src={api.outputDownloadUrl(jobId, finalVideo.output_id)} poster="" preload="metadata"></video>
      </div>
    </section>
  {/if}

  {#if sceneClips.length > 0}
    <section class="vf-card">
      <header class="card-header">
        <div>
          <h2>Scene clips</h2>
          <p class="muted">{sceneClips.length} per-scene asset{sceneClips.length === 1 ? '' : 's'} produced by the mock renderer.</p>
        </div>
      </header>
      <div class="clip-grid">
        {#each sceneClips as c}
          <a href={api.outputDownloadUrl(jobId, c.output_id)} download={c.filename} class="clip hoverable">
            <span class="clip-name">{c.filename}</span>
            <span class="muted mono">{Math.round(c.size_bytes / 1024)} KB</span>
          </a>
        {/each}
      </div>
    </section>
  {/if}

  <section class="vf-card">
    <header class="card-header">
      <div>
        <h2>Publishing</h2>
        <p class="muted">Mock-mode. Live Publer integration is disabled until a verified adapter is configured.</p>
      </div>
    </header>

    {#if job.state === 'done'}
      <form class="publish-form" onsubmit={(e) => { e.preventDefault(); publish(); }}>
        <div class="vf-row">
          <label class="vf-col">
            <span class="lbl">Action</span>
            <select bind:value={publishForm.action}>
              <option value="draft">Save draft</option>
              <option value="schedule">Schedule</option>
              <option value="publish">Publish now</option>
            </select>
          </label>
          <label class="vf-col" style="flex: 2 1 320px;">
            <span class="lbl">Platforms</span>
            <input bind:value={publishForm.platforms} placeholder="tiktok, instagram, linkedin" />
          </label>
          {#if publishForm.action === 'schedule'}
            <label class="vf-col">
              <span class="lbl">Scheduled for (ISO)</span>
              <input type="text" bind:value={publishForm.scheduled_for} placeholder="2026-12-31T09:00:00Z" />
            </label>
          {/if}
        </div>
        <label class="full-lbl">
          <span class="lbl">Caption</span>
          <textarea bind:value={publishForm.caption} rows="2" placeholder="Try it tomorrow — link in bio"></textarea>
        </label>
        <div class="actions">
          <button type="submit" class="vf-btn vf-btn-primary">Submit</button>
          {#if publishResult}
            <span class="vf-pill {publishResult.ok ? 'ok' : 'fail'}"><span class="dot"></span>{publishResult.detail}</span>
          {/if}
        </div>
      </form>

      {#if publishIntents.length > 0}
        <h3 class="section-h3">Past intents</h3>
        <table>
          <thead><tr><th>Action</th><th>Platforms</th><th>Caption</th><th>Status</th><th>When</th></tr></thead>
          <tbody>
            {#each publishIntents as p}
              <tr>
                <td><span class="vf-pill {p.action === 'publish' ? 'ok' : p.action === 'schedule' ? 'run' : 'info'}"><span class="dot"></span>{p.action}</span></td>
                <td><span class="mono">{p.platforms.join(', ')}</span></td>
                <td class="caption-cell">{p.caption || '—'}</td>
                <td><span class="vf-pill ok"><span class="dot"></span>{p.status}</span></td>
                <td class="muted mono">{p.executed_at || p.created_at}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {:else}
      <p class="muted">Job must reach <code>done</code> before publishing. Currently <code>{job.state}</code>.</p>
    {/if}
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
  .eyebrow .mono { color: var(--vf-text-secondary); margin-left: var(--vf-sp-2); }
  .link-back { color: var(--vf-text-secondary); }
  .link-back:hover { color: var(--vf-text-primary); }
  .sub { color: var(--vf-text-secondary); max-width: 720px; margin-top: var(--vf-sp-3); }
  .sub .mono { font-size: var(--vf-fs-cap); }
  .muted { color: var(--vf-text-secondary); }
  .mono { font-family: var(--vf-font-mono); font-size: var(--vf-fs-cap); }
  .header-actions { display: flex; gap: var(--vf-sp-3); align-items: center; }
  .status-msg {
    margin-top: var(--vf-sp-3);
    color: var(--vf-text-secondary);
    font-size: var(--vf-fs-ui);
    font-family: var(--vf-font-mono);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--vf-sp-3);
    margin-bottom: var(--vf-sp-5);
  }
  .section-h3 {
    font-size: var(--vf-fs-titles);
    margin: var(--vf-sp-6) 0 var(--vf-sp-3);
    color: var(--vf-text-primary);
  }

  /* Timeline */
  .timeline {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--vf-sp-3);
    position: relative;
  }
  .timeline::before {
    content: "";
    position: absolute;
    left: 19px;
    top: 20px;
    bottom: 20px;
    width: 1px;
    background: var(--vf-border);
  }
  .stage {
    display: flex;
    gap: var(--vf-sp-4);
    padding: var(--vf-sp-3);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    transition:
      border-color var(--vf-dur) var(--vf-ease),
      background var(--vf-dur) var(--vf-ease);
    position: relative;
  }
  .stage.running {
    border-color: rgba(255, 206, 71, 0.45);
    background: var(--vf-warning-surface);
  }
  .stage.done {
    border-color: rgba(110, 232, 110, 0.25);
  }
  .stage.failed {
    border-color: rgba(248, 113, 113, 0.25);
    background: var(--vf-error-surface);
  }
  .bullet {
    flex-shrink: 0;
    width: 40px;
    height: 40px;
    border-radius: 999px;
    background: var(--vf-surface-3);
    border: 1px solid var(--vf-border);
    display: grid;
    place-items: center;
    z-index: 1;
  }
  .stage.done .bullet {
    background: var(--vf-success);
    border-color: var(--vf-success);
    color: var(--vf-bg);
  }
  .stage.running .bullet {
    background: var(--vf-warning);
    border-color: var(--vf-warning);
    color: var(--vf-bg);
    animation: pulse 1.4s ease-in-out infinite;
  }
  .stage.failed .bullet {
    background: var(--vf-error);
    border-color: var(--vf-error);
    color: var(--vf-bg);
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(255,206,71,0.4); }
    50% { transform: scale(1.05); box-shadow: 0 0 0 6px rgba(255,206,71,0); }
  }
  .num {
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-cap);
    font-weight: var(--vf-fw-bold);
  }
  .stage-body { flex: 1; min-width: 0; }
  .stage-head {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-3);
    flex-wrap: wrap;
  }
  .stage-name {
    text-transform: capitalize;
    font-weight: var(--vf-fw-medium);
    font-size: var(--vf-fs-panels);
  }
  .stage-error {
    margin-top: var(--vf-sp-2);
    padding: var(--vf-sp-2) var(--vf-sp-3);
    background: rgba(248, 113, 113, 0.1);
    color: var(--vf-error-text);
    border-radius: var(--vf-radius-sm);
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-cap);
  }
  .stage-details {
    margin-top: var(--vf-sp-2);
    font-size: var(--vf-fs-cap);
    color: var(--vf-text-secondary);
  }
  .stage-details summary {
    cursor: pointer;
    user-select: none;
    padding: 2px 0;
  }
  .stage-details pre {
    margin: var(--vf-sp-2) 0 0;
    padding: var(--vf-sp-3);
    background: var(--vf-bg);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-sm);
    overflow: auto;
    max-height: 240px;
    font-size: var(--vf-fs-cap);
    font-family: var(--vf-font-mono);
    color: var(--vf-text-secondary);
  }

  .clip-grid {
    display: grid;
    gap: var(--vf-sp-2);
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  }
  .clip {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--vf-sp-3);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    text-decoration: none;
    color: inherit;
    transition:
      transform var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
  }
  .clip.hoverable:hover { transform: translateY(-1px); border-color: var(--vf-butter-green); }
  .clip-name {
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-cap);
  }

  .player {
    background: #000;
    border-radius: var(--vf-radius-md);
    overflow: hidden;
    border: 1px solid var(--vf-border);
  }
  .player video {
    width: 100%;
    height: auto;
    max-height: 480px;
    background: #000;
  }

  .publish-form .vf-row { margin-bottom: var(--vf-sp-2); align-items: end; }
  .publish-form .vf-col {
    display: flex;
    flex-direction: column;
    gap: var(--vf-sp-2);
  }
  .publish-form .full-lbl {
    display: flex;
    flex-direction: column;
    gap: var(--vf-sp-2);
    margin-top: var(--vf-sp-2);
  }
  .publish-form .lbl {
    font-size: var(--vf-fs-cap-sm);
    font-weight: var(--vf-fw-medium);
    color: var(--vf-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .publish-form textarea { margin-bottom: var(--vf-sp-4); }
  .actions {
    display: flex;
    gap: var(--vf-sp-3);
    align-items: center;
    margin-top: var(--vf-sp-3);
  }
  .caption-cell {
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>