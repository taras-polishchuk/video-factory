<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';

  let jobs = $state([]);
  let companies = $state([]);
  let filter = $state({ company: '', state: '' });

  async function load() {
    companies = await api.listCompanies();
    jobs = await api.listJobs();
  }
  onMount(load);

  let filtered = $derived(jobs.filter(j =>
    (!filter.company || j.company_id === filter.company) &&
    (!filter.state || j.state === filter.state)
  ));
</script>

<svelte:head><title>Jobs · Video Factory</title></svelte:head>

<h1>Jobs</h1>

<div class="card row">
  <div class="col"><label>Company</label>
    <select bind:value={filter.company}>
      <option value="">all</option>
      {#each companies as c}<option value={c.company_id}>{c.name}</option>{/each}
    </select>
  </div>
  <div class="col"><label>State</label>
    <select bind:value={filter.state}>
      <option value="">all</option>
      <option value="queued">queued</option>
      <option value="running">running</option>
      <option value="done">done</option>
      <option value="failed">failed</option>
    </select>
  </div>
</div>

<div class="card">
  {#if filtered.length === 0}
    <p style="color: var(--text-dim);">No jobs match.</p>
  {:else}
    <table>
      <thead><tr><th>Job</th><th>Topic</th><th>Company</th><th>State</th><th>Stage</th><th>Created</th></tr></thead>
      <tbody>
        {#each filtered as j}
          <tr>
            <td><a href="/jobs/{j.job_id}" style="color: var(--accent);">{j.job_id}</a></td>
            <td>{j.topic}</td>
            <td style="color: var(--text-dim);">{j.company_id}</td>
            <td><span class="pill {j.state === 'done' ? 'ok' : j.state === 'failed' ? 'fail' : 'run'}">{j.state}</span></td>
            <td style="color: var(--text-dim);">{j.current_stage || '—'}</td>
            <td style="color: var(--text-dim);">{j.created_at}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>