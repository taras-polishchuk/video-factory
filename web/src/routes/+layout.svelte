<script>
  import { api } from '$lib/api.js';
  let { children } = $props();
  let companies = $state([]);
  let selectedId = $state('');
  let company = $derived(companies.find(c => c.company_id === selectedId) || companies[0]);
  let integrations = $state([]);
  let health = $state(null);
  let navOpen = $state(true);

  $effect(() => {
    api.health().then(h => health = h).catch(() => health = { ok: false });
    api.listCompanies().then(cs => {
      companies = cs;
      const saved = typeof localStorage !== 'undefined' && localStorage.getItem('vf.company_id');
      const found = saved && cs.find(c => c.company_id === saved);
      selectedId = (found || cs[0] || {}).company_id || '';
    });
    api.integrations().then(set => integrations = set);
  });

  function setCompany(c) {
    company = c;
    selectedId = c.company_id;
    if (typeof localStorage !== 'undefined') localStorage.setItem('vf.company_id', c.company_id);
  }

  async function createCompany() {
    const slug = prompt('company slug (lowercase, no spaces)');
    if (!slug) return;
    const name = prompt('display name');
    if (!name) return;
    const c = await api.createCompany({ slug, name });
    companies = [...companies, c];
    setCompany(c);
  }
</script>

<div class="layout">
  <aside class="sidebar" class:collapsed={!navOpen}>
    <button class="toggle" onclick={() => navOpen = !navOpen} aria-label="Toggle sidebar">
      {navOpen ? '◀' : '▶'}
    </button>

    <div class="brand">
      <div class="logo">VF</div>
      {#if navOpen}
        <div class="brand-text">
          <div class="brand-title">Video Factory</div>
          <div class="brand-sub">Control Room · {health?.mock_only ? 'mock' : 'live'}</div>
        </div>
      {/if}
    </div>

    {#if navOpen}
      <div class="company-picker">
        <label>Workspace</label>
        <select bind:value={selectedId}>
          {#each companies as c}
            <option value={c.company_id}>{c.name}</option>
          {/each}
        </select>
        <button class="ghost" onclick={createCompany}>+ New</button>
      </div>

      <nav>
        <a href="/">Overview</a>
        <a href="/bible">Video Bible</a>
        <a href="/assets">Assets</a>
        <a href="/jobs">Jobs</a>
        <a href="/create">Create video</a>
        <a href="/integrations">Integrations</a>
      </nav>

      <div class="integrations-strip">
        {#each integrations as i}
          <div class="chip {i.enabled ? 'on' : 'off'}" title={i.detail}>
            <span class="dot"></span>{i.name}
          </div>
        {/each}
      </div>
    {/if}
  </aside>

  <main>
    {#if company}
      {@render children()}
    {:else}
      <div class="empty">
        <h2>No workspace yet</h2>
        <p>Create a company to start producing.</p>
        <button onclick={createCompany}>+ Create workspace</button>
      </div>
    {/if}
  </main>
</div>

<style>
  :global(:root) {
    --bg: #0a0e14;
    --bg-elev: #121821;
    --bg-elev-2: #1a2230;
    --border: #232b3a;
    --text: #e4e9f0;
    --text-dim: #8a96a8;
    --accent: #f59e0b;
    --accent-2: #0d9488;
    --ok: #22c55e;
    --warn: #f59e0b;
    --err: #ef4444;
    --muted: #475569;
  }
  :global(*) { box-sizing: border-box; }
  :global(html, body) {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.5;
  }
  :global(a) { color: inherit; text-decoration: none; }
  :global(button) {
    font: inherit;
    background: var(--accent);
    color: #0a0e14;
    border: 0;
    border-radius: 6px;
    padding: 8px 14px;
    cursor: pointer;
    font-weight: 600;
  }
  :global(button.ghost) {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border);
  }
  :global(button:hover) { filter: brightness(1.1); }
  :global(input, select, textarea) {
    font: inherit;
    background: var(--bg-elev-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 10px;
    width: 100%;
  }
  :global(.card) {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 16px;
  }
  :global(.row) { display: flex; gap: 16px; flex-wrap: wrap; }
  :global(.col) { flex: 1 1 200px; min-width: 0; }
  :global(h1) { font-size: 24px; font-weight: 700; margin: 0 0 8px; letter-spacing: -0.01em; }
  :global(h2) { font-size: 18px; font-weight: 700; margin: 0 0 12px; }
  :global(label) { font-size: 12px; color: var(--text-dim); display: block; margin-bottom: 4px; }
  :global(table) { width: 100%; border-collapse: collapse; }
  :global(th), :global(td) {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  :global(th) { color: var(--text-dim); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  :global(.pill) {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  :global(.pill.ok) { background: rgba(34,197,94,0.15); color: var(--ok); }
  :global(.pill.run) { background: rgba(245,158,11,0.15); color: var(--warn); }
  :global(.pill.fail) { background: rgba(239,68,68,0.15); color: var(--err); }
  :global(.pill.muted) { background: var(--bg-elev-2); color: var(--text-dim); }

  .layout { display: flex; min-height: 100vh; }
  .sidebar {
    width: 240px;
    background: var(--bg-elev);
    border-right: 1px solid var(--border);
    padding: 20px 16px;
    display: flex;
    flex-direction: column;
    gap: 18px;
    transition: width 0.2s ease;
  }
  .sidebar.collapsed { width: 56px; padding: 20px 8px; }
  .toggle {
    align-self: flex-end;
    background: transparent;
    color: var(--text-dim);
    padding: 2px 6px;
    border: 0;
  }
  .brand { display: flex; gap: 12px; align-items: center; }
  .logo {
    width: 32px; height: 32px;
    border-radius: 8px;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    display: grid; place-items: center;
    font-weight: 800;
    color: #0a0e14;
    font-size: 14px;
    flex-shrink: 0;
  }
  .brand-title { font-weight: 700; font-size: 15px; }
  .brand-sub { font-size: 11px; color: var(--text-dim); }
  .company-picker { display: flex; flex-direction: column; gap: 6px; }
  .company-picker select { width: 100%; }
  .company-picker button { width: 100%; }
  nav { display: flex; flex-direction: column; gap: 2px; }
  nav a {
    padding: 8px 10px;
    border-radius: 6px;
    color: var(--text-dim);
    font-weight: 500;
  }
  nav a:hover { background: var(--bg-elev-2); color: var(--text); }
  .integrations-strip {
    margin-top: auto;
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .chip {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 10px;
    background: var(--bg-elev-2);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .chip .dot {
    width: 6px; height: 6px;
    border-radius: 999px;
    background: var(--muted);
  }
  .chip.on .dot { background: var(--ok); }
  main { flex: 1; padding: 32px 40px; min-width: 0; }
  .empty { padding: 80px 40px; text-align: center; color: var(--text-dim); }
  .empty button { margin-top: 16px; }
</style>