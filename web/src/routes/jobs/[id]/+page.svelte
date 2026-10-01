<script>
  import { api } from '$lib/api.js';
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';

  let jobId = $derived($page.params.id);
  let job = $state(null);
  let stages = $state([]);
  let outputs = $state([]);
  let timer;

  async function load() {
    try {
      job = await api.getJob(jobId);
      stages = await api.jobStages(jobId);
      outputs = await api.jobOutputs(jobId);
    } catch (e) {
      job = null;
    }
  }

  onMount(() => {
    load();
    timer = setInterval(load, 1500);
  });
  onDestroy(() => clearInterval(timer));

  let finalVideo = $derived(outputs.find(o => o.kind === 'final_video'));
  let sceneClips = $derived(outputs.filter(o => o.kind === 'scene_clip'));
</script>

<svelte:head><title>{job?.topic || 'Job'} · Video Factory</title></svelte:head>

{#if !job}
  <p style="color: var(--err);">Job not found.</p>
{:else}
  <h1>{job.topic}</h1>
  <p style="color: var(--text-dim); margin-top: 0;">
    {job.job_id} · {job.duration_seconds}s · {job.aspect_ratio} · {job.platforms.join(', ')}
  </p>

  <div class="card">
    <div style="display: flex; gap: 16px; align-items: center;">
      <span class="pill {job.state === 'done' ? 'ok' : job.state === 'failed' ? 'fail' : 'run'}">{job.state}</span>
      <span style="color: var(--text-dim);">{job.status_message || '—'}</span>
      <span style="flex: 1;"></span>
      {#if job.state !== 'done' && job.state !== 'running'}
        <button class="ghost" onclick={() => api.retryJob(jobId).then(load)}>Retry</button>
      {/if}
    </div>
  </div>

  <div class="card">
    <h2>Pipeline timeline</h2>
    <div class="timeline">
      {#each stages as s}
        <div class="stage {s.status}">
          <div class="bullet"></div>
          <div class="content">
            <div class="stage-key">{s.ordinal}. {s.stage_key.replace(/_/g, ' ')}</div>
            <div class="stage-meta">
              <span class="pill {s.status === 'done' ? 'ok' : s.status === 'failed' ? 'fail' : s.status === 'running' ? 'run' : 'muted'}">{s.status}</span>
              {#if s.duration_ms !== null && s.duration_ms !== undefined}
                <span style="color: var(--text-dim); font-size: 11px;">{s.duration_ms} ms</span>
              {/if}
            </div>
            {#if s.error}
              <div style="color: var(--err); font-size: 12px; margin-top: 4px;">{s.error}</div>
            {/if}
            {#if s.summary && Object.keys(s.summary).length > 0}
              <details style="margin-top: 4px;">
                <summary style="cursor: pointer; color: var(--text-dim); font-size: 12px;">details</summary>
                <pre style="font-size: 11px; overflow: auto; max-height: 160px; background: var(--bg); padding: 8px; border-radius: 4px;">{JSON.stringify(s.summary, null, 2)}</pre>
              </details>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  </div>

  {#if finalVideo}
    <div class="card">
      <h2>Final video</h2>
      <video controls src={api.outputDownloadUrl(jobId, finalVideo.output_id)} style="max-width: 100%; border-radius: 8px; background: #000;"></video>
      <div style="margin-top: 12px; display: flex; gap: 12px;">
        <a href={api.outputDownloadUrl(jobId, finalVideo.output_id)} download={finalVideo.filename}>
          <button>Download {finalVideo.filename}</button>
        </a>
        <span style="color: var(--text-dim); font-size: 12px; align-self: center;">
          {Math.round(finalVideo.size_bytes / 1024)} KB · mock
        </span>
      </div>
    </div>
  {/if}

  {#if sceneClips.length > 0}
    <div class="card">
      <h2>Scene clips ({sceneClips.length})</h2>
      <table>
        <thead><tr><th>File</th><th>Size</th></tr></thead>
        <tbody>
          {#each sceneClips as c}
            <tr>
              <td>{c.filename}</td>
              <td style="color: var(--text-dim);">{Math.round(c.size_bytes / 1024)} KB</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <div class="card">
    <h2>Publishing</h2>
    <p style="color: var(--text-dim);">Mock mode. Live Publer integration is disabled until a verified adapter is configured.</p>
    {#if job.state === 'done'}
      {@const intentAction = ''}
      <div class="row">
        <button class="ghost" onclick={() => api.publish(jobId, {action:'draft', platforms:['tiktok']}).then(load)}>Save draft</button>
        <button class="ghost" onclick={() => api.publish(jobId, {action:'schedule', platforms:['ig'], scheduled_for:'2026-12-31T09:00:00Z'}).then(load)}>Schedule</button>
        <button onclick={() => api.publish(jobId, {action:'publish', platforms:['tiktok','linkedin']}).then(load)}>Publish now</button>
      </div>
    {:else}
      <p style="color: var(--warn);">Job must be in <code>done</code> state to publish. Currently: <code>{job.state}</code></p>
    {/if}
  </div>
{/if}

<style>
  .timeline { display: flex; flex-direction: column; gap: 12px; }
  .stage { display: flex; gap: 14px; align-items: flex-start; }
  .bullet {
    width: 12px; height: 12px;
    border-radius: 999px;
    background: var(--muted);
    flex-shrink: 0;
    margin-top: 6px;
  }
  .stage.done .bullet { background: var(--ok); }
  .stage.running .bullet { background: var(--warn); animation: pulse 1.4s infinite; }
  .stage.failed .bullet { background: var(--err); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .content { flex: 1; min-width: 0; }
  .stage-key { font-weight: 600; text-transform: capitalize; }
  .stage-meta { display: flex; gap: 8px; align-items: center; margin-top: 2px; }
  pre { white-space: pre-wrap; word-break: break-all; }
</style>