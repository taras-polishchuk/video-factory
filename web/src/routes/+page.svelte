<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';

  let stats = $state({ companies: 0, jobs: 0, running: 0, done: 0, failed: 0, integrations: 0 });
  let recent = $state([]);
  let integrations = $state([]);
  let loaded = $state(false);

  onMount(async () => {
    const cs = await api.listCompanies();
    const jobs = cs.length ? await api.listJobs(cs[0].company_id) : await api.listJobs();
    const ints = await api.integrations();
    integrations = ints;
    stats = {
      companies: cs.length,
      jobs: jobs.length,
      running: jobs.filter(j => j.state === 'running' || j.state === 'queued').length,
      done: jobs.filter(j => j.state === 'done').length,
      failed: jobs.filter(j => j.state === 'failed').length,
      integrations: ints.length,
    };
    recent = jobs.slice(0, 6);
    loaded = true;
  });

  function pillFor(state) {
    if (state === 'done') return 'ok';
    if (state === 'failed') return 'fail';
    if (state === 'queued' || state === 'running') return 'run';
    return 'muted';
  }
</script>

<svelte:head><title>Overview · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">Workspace overview</p>
      <h1>Today’s production</h1>
      <p class="sub">A snapshot of every workspace, queue, and integration status. Mock-mode renders run locally; live providers stay disabled until a verified adapter is configured.</p>
    </div>
    <div class="header-actions">
      <a href="/create" class="vf-btn vf-btn-primary">+ New video</a>
      <a href="/integrations" class="vf-btn vf-btn-ghost">Integrations</a>
    </div>
  </div>
</header>

<section class="stat-grid">
  <a href="/bible" class="stat hoverable">
    <span class="stat-label">Companies</span>
    <span class="stat-value">{stats.companies}</span>
    <span class="stat-trend">brand bible, assets, scopes</span>
  </a>
  <a href="/jobs" class="stat hoverable">
    <span class="stat-label">Jobs total</span>
    <span class="stat-value">{stats.jobs}</span>
    <span class="stat-trend">all states, all workspaces</span>
  </a>
  <div class="stat hoverable">
    <span class="stat-label">Active</span>
    <span class="stat-value">{stats.running}</span>
    <span class="stat-trend"><span class="vf-pill run"><span class="dot"></span>queued + running</span></span>
  </div>
  <div class="stat hoverable">
    <span class="stat-label">Done</span>
    <span class="stat-value">{stats.done}</span>
    <span class="stat-trend"><span class="vf-pill ok"><span class="dot"></span>ready to publish</span></span>
  </div>
</section>

<section class="vf-card">
  <header class="card-header">
    <div>
      <h2>Recent jobs</h2>
      <p class="muted">Latest jobs across every workspace, regardless of monitor.</p>
    </div>
    <a href="/jobs" class="vf-btn vf-btn-ghost vf-btn-sm">View all →</a>
  </header>

  {#if !loaded}
    <p class="muted">Loading…</p>
  {:else if recent.length === 0}
    <div class="empty">
      <p>No jobs yet.</p>
      <a href="/create" class="vf-btn vf-btn-primary vf-btn-sm">+ Create the first one</a>
    </div>
  {:else}
    <table>
      <thead>
        <tr><th>Job</th><th>Topic</th><th>Company</th><th>State</th><th>Stage</th><th>Created</th></tr>
      </thead>
      <tbody>
        {#each recent as j}
          <tr>
            <td><a class="link" href="/jobs/{j.job_id}">{j.job_id}</a></td>
            <td>{j.topic}</td>
            <td class="mono">{j.company_id}</td>
            <td><span class="vf-pill {pillFor(j.state)}"><span class="dot"></span>{j.state}</span></td>
            <td class="muted">{j.current_stage || '—'}</td>
            <td class="muted mono">{j.created_at}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<section class="vf-card vf-card-glow">
  <header class="card-header">
    <div>
      <h2>Provider connections</h2>
      <p class="muted">Live integrations stay disabled until a separately verified adapter is in place.</p>
    </div>
    <a href="/integrations" class="vf-btn vf-btn-ghost vf-btn-sm">Manage</a>
  </header>

  {#if integrations.length === 0}
    <p class="muted">Loading provider status…</p>
  {:else}
    <div class="provider-row">
      {#each integrations as i}
        <div class="provider hoverable" class:enabled={i.enabled}>
          <span class="provider-dot"></span>
          <span class="provider-name">{i.name}</span>
          <span class="vf-pill {i.enabled ? 'ok' : 'muted'}"><span class="dot"></span>{i.mode}</span>
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .page-header {
    margin-bottom: var(--vf-sp-8);
  }
  .header-row {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
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
  .sub {
    color: var(--vf-text-secondary);
    max-width: 640px;
    margin-top: var(--vf-sp-3);
  }
  .header-actions {
    display: flex;
    gap: var(--vf-sp-2);
  }

  .stat-grid {
    display: grid;
    gap: var(--vf-sp-4);
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    margin-bottom: var(--vf-sp-6);
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: var(--vf-sp-2);
    padding: var(--vf-sp-5) var(--vf-sp-5);
    background: var(--vf-surface-1);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    text-decoration: none;
    color: inherit;
    transition:
      transform var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
    position: relative;
    overflow: hidden;
  }
  .stat::after {
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(110,232,110,0.4), transparent);
    opacity: 0;
    transition: opacity var(--vf-dur) var(--vf-ease);
  }
  .stat.hoverable:hover {
    transform: translateY(-2px);
    border-color: var(--vf-border-default);
  }
  .stat.hoverable:hover::after { opacity: 1; }
  .stat-label {
    font-size: var(--vf-fs-cap-sm);
    color: var(--vf-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .stat-value {
    font-size: 36px;
    font-weight: var(--vf-fw-light);
    line-height: 1;
    color: var(--vf-text-primary);
    font-variant-numeric: tabular-nums;
  }
  .stat-trend { font-size: var(--vf-fs-cap); color: var(--vf-text-secondary); }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--vf-sp-3);
    margin-bottom: var(--vf-sp-4);
  }
  .card-header h2 { font-size: var(--vf-fs-titles); }
  .card-header .muted { margin-top: 4px; }

  .empty {
    text-align: center;
    padding: var(--vf-sp-6);
    color: var(--vf-text-secondary);
  }
  .empty .vf-btn { margin-top: var(--vf-sp-3); }

  table { margin-top: var(--vf-sp-3); }
  .link { color: var(--vf-butter-green); font-family: var(--vf-font-mono); font-size: var(--vf-fs-cap); }
  .link:hover { text-decoration: underline; }
  .muted { color: var(--vf-text-secondary); }
  .mono { font-family: var(--vf-font-mono); font-size: var(--vf-fs-cap); }

  .provider-row {
    display: grid;
    gap: var(--vf-sp-2);
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    margin-top: var(--vf-sp-3);
  }
  .provider {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-3);
    padding: var(--vf-sp-3) var(--vf-sp-4);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    transition:
      transform var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
  }
  .provider.hoverable:hover { transform: translateY(-1px); border-color: var(--vf-border-default); }
  .provider-dot {
    width: 8px; height: 8px;
    border-radius: 999px;
    background: var(--vf-text-disabled);
    flex-shrink: 0;
  }
  .provider.enabled .provider-dot {
    background: var(--vf-success);
    box-shadow: 0 0 0 3px rgba(110, 232, 110, 0.15);
  }
  .provider-name {
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-cap);
    flex: 1;
    color: var(--vf-text-primary);
  }

  @media (max-width: 720px) {
    .stat-value { font-size: 28px; }
  }
</style>