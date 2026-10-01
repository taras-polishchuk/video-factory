<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  let stats = $state({ companies: 0, jobs: 0, running: 0, done: 0, integrations: 0 });
  let recent = $state([]);

  onMount(async () => {
    const cs = await api.listCompanies();
    const jobs = await api.listJobs();
    stats = {
      companies: cs.length,
      jobs: jobs.length,
      running: jobs.filter(j => j.state === 'running' || j.state === 'queued').length,
      done: jobs.filter(j => j.state === 'done').length,
      integrations: (await api.integrations()).length,
    };
    recent = jobs.slice(0, 5);
  });
</script>

<svelte:head><title>Overview · Video Factory</title></svelte:head>

<h1>Overview</h1>
<p style="color: var(--text-dim); margin-top: 0;">Production state across the workspace. Mock-only by default; live providers stay disabled until a verified adapter is configured.</p>

<div class="row">
  <div class="card col">
    <label>Companies</label>
    <div class="big">{stats.companies}</div>
  </div>
  <div class="card col">
    <label>Total jobs</label>
    <div class="big">{stats.jobs}</div>
  </div>
  <div class="card col">
    <label>Active</label>
    <div class="big">{stats.running}</div>
  </div>
  <div class="card col">
    <label>Done</label>
    <div class="big">{stats.done}</div>
  </div>
</div>

<div class="card">
  <h2>Recent jobs</h2>
  {#if recent.length === 0}
    <p style="color: var(--text-dim);">No jobs yet. <a href="/create" style="color: var(--accent);">Create the first one →</a></p>
  {:else}
    <table>
      <thead><tr><th>Job</th><th>Topic</th><th>State</th><th>Stage</th></tr></thead>
      <tbody>
        {#each recent as j}
          <tr>
            <td><a href="/jobs/{j.job_id}" style="color: var(--accent);">{j.job_id}</a></td>
            <td>{j.topic}</td>
            <td><span class="pill {j.state === 'done' ? 'ok' : j.state === 'failed' ? 'fail' : 'run'}">{j.state}</span></td>
            <td style="color: var(--text-dim);">{j.current_stage || '—'}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .big { font-size: 32px; font-weight: 700; letter-spacing: -0.02em; margin-top: 4px; }
</style>