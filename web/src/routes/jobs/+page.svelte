<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';

  let jobs = $state([]);
  let companies = $state([]);
  let filter = $state({ company: '', state: '' });
  let loaded = $state(false);

  async function load() {
    companies = await api.listCompanies();
    if (companies.length && !filter.company) {
      filter.company = companies[0].company_id;
    }
    jobs = filter.company
      ? await api.listJobs(filter.company)
      : await api.listJobs();
    loaded = true;
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
    <span class="muted small">Company</span>
    <select bind:value={filter.company}>
      <option value="">All companies</option>
      {#each companies as c}<option value={c.company_id}>{c.name}</option>{/each}
    </select>
  </div>
  <div class="filter-cell">
    <span class="muted small">State</span>
    <select bind:value={filter.state}>
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
  {#if !loaded}
    <p class="muted">Loading…</p>
  {:else if filtered.length === 0}
    <div class="empty">
      <p>No jobs match.</p>
      <p class="muted">Try clearing the filters above.</p>
    </div>
  {:else}
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
    border-radius: var(--radius-pill);
    padding: 2px 8px;
    color: var(--vf-text-secondary);
  }
</style>