<script>
  import { api, describeError } from '$lib/api-client.js';
  import StatusNote from '$lib/components/StatusNote.svelte';
  import { onMount } from 'svelte';

  let { data } = $props();

  let jobs = $state([]);
  let filter = $state({ company: '', state: '' });
  let phase = $state('loading'); // loading | ready | error
  let loadError = $state('');

  async function load() {
    if (!data.activeWorkspace) return;
    if (!filter.company) filter.company = data.activeWorkspace.workspace_id;
    phase = 'loading';
    loadError = '';
    try {
      jobs = await api.listJobs(filter.company);
      phase = 'ready';
    } catch (e) {
      loadError = describeError(e);
      phase = 'error';
    }
  }
  onMount(load);

  let filtered = $derived(jobs.filter(j =>
    (!filter.company || j.company_id === filter.company) &&
    (!filter.state || j.state === filter.state)
  ));

  function pillFor(state) {
    if (state === 'done') return 'ok';
    if (state === 'failed') return 'fail';
    if (state === 'queued' || state === 'running') return 'run';
    return 'muted';
  }
</script>

<svelte:head><title>Jobs · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">Pipeline</p>
      <h1>Jobs</h1>
      <p class="sub">Every job across the active workspace. Click a row to open its timeline, stages, and downloadable outputs.</p>
    </div>
    <div class="header-actions">
      <a href="/create" class="vf-btn vf-btn-primary">+ New job</a>
    </div>
  </div>
</header>

<section class="vf-card filters">
  <div class="filter-cell">
    <label class="muted small" for="jobs-filter-company">Company</label>
    <select id="jobs-filter-company" name="company" bind:value={filter.company}>
      <option value="">All workspaces</option>
      {#each (data.workspaces || []) as c}<option value={c.workspace_id}>{c.name}</option>{/each}
    </select>
  </div>
  <div class="filter-cell">
    <label class="muted small" for="jobs-filter-state">State</label>
    <select id="jobs-filter-state" name="state" bind:value={filter.state}>
      <option value="">All states</option>
      <option value="queued">Queued</option>
      <option value="running">Running</option>
      <option value="done">Done</option>
      <option value="failed">Failed</option>
    </select>
  </div>
  <div class="filter-count">
    <span class="muted small">Showing</span>
    <span class="vf-pill muted"><span class="dot"></span>{filtered.length} / {jobs.length}</span>
  </div>
</section>

<section class="vf-card">
  {#if !data.activeWorkspace}
    <StatusNote kind="empty" title="No workspace yet" message="Jobs belong to a workspace. Join or create one first." />
  {:else if phase === 'loading'}
    <StatusNote kind="loading" message="Loading jobs…" />
  {:else if phase === 'error'}
    <StatusNote kind="error" title="Couldn’t load jobs" message={loadError} onretry={load} />
  {:else if jobs.length === 0}
    <div class="empty">
      <p>No jobs yet.</p>
      <a href="/create" class="vf-btn vf-btn-primary vf-btn-sm">+ Create the first one</a>
    </div>
  {:else if filtered.length === 0}
    <div class="empty">
      <p>No jobs match.</p>
      <p class="muted">Try clearing the filters above.</p>
    </div>
  {:else}
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Job</th>
          <th>Topic</th>
          <th>Company</th>
          <th>Platforms</th>
          <th>State</th>
          <th>Stage</th>
          <th>Created</th>
        </tr>
      </thead>
      <tbody>
        {#each filtered as j}
          <tr class="row-link">
            <td><a class="link" href="/jobs/{j.job_id}">{j.job_id}</a></td>
            <td>{j.topic}</td>
            <td class="mono">{j.company_id}</td>
            <td>
                <span class="platforms">
                  {#each j.platforms as p}<span class="platform-tag">{p}</span>{/each}
                </span>
            </td>
            <td><span class="vf-pill {pillFor(j.state)}"><span class="dot"></span>{j.state}</span></td>
            <td class="muted">{j.current_stage || '—'}</td>
            <td class="muted mono">{j.created_at}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    </div>
  {/if}
</section>

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
  .muted { color: var(--vf-text-secondary); }
  .mono { font-family: var(--vf-font-mono); font-size: var(--vf-fs-cap); }
  .small { font-size: var(--vf-fs-cap-sm); text-transform: uppercase; letter-spacing: 0.06em; }

  .filters {
    display: flex;
    align-items: end;
    gap: var(--vf-sp-4);
    flex-wrap: wrap;
    padding: var(--vf-sp-4);
    margin-bottom: var(--vf-sp-4);
  }
  .filter-cell { display: flex; flex-direction: column; gap: var(--vf-sp-2); min-width: 180px; }
  .filter-cell select { width: 100%; }
  .filter-count { margin-left: auto; }

  .empty { text-align: center; padding: var(--vf-sp-7); color: var(--vf-text-secondary); }

  table { margin-top: var(--vf-sp-2); }
  .link { color: var(--vf-butter-green); font-family: var(--vf-font-mono); font-size: var(--vf-fs-cap); }
  .link:hover { text-decoration: underline; }
  .row-link { cursor: pointer; }
  .row-link:hover a { text-decoration: underline; }

  .platforms { display: inline-flex; flex-wrap: wrap; gap: 4px; }
  .platform-tag {
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-cap-sm);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-pill);
    padding: 2px 8px;
    color: var(--vf-text-secondary);
  }
</style>