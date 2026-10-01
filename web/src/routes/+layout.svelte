<script>
  import '../app.css';
  import { api } from '$lib/api.js';

  let { children } = $props();

  let companies = $state([]);
  let selectedId = $state('');
  let company = $derived(companies.find(c => c.company_id === selectedId) || companies[0]);
  let integrations = $state([]);
  let health = $state(null);
  let mobileNavOpen = $state(false);
  let workspaceMenuOpen = $state(false);

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
    selectedId = c.company_id;
    workspaceMenuOpen = false;
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

  function closeMenus() { workspaceMenuOpen = false; }

  const navItems = [
    { href: '/', label: 'Overview' },
    { href: '/bible', label: 'Video Bible' },
    { href: '/assets', label: 'Assets' },
    { href: '/jobs', label: 'Jobs' },
    { href: '/create', label: 'Create' },
    { href: '/integrations', label: 'Integrations' }
  ];
</script>

<svelte:window on:click={closeMenus} />

<div class="app">
  <header class="topbar">
    <div class="topbar-left">
      <button class="vf-mobile-toggle" onclick={() => mobileNavOpen = !mobileNavOpen} aria-label="Open menu">
        <span></span><span></span><span></span>
      </button>

      <a href="/" class="brand" onclick={() => mobileNavOpen = false}>
        <span class="brand-mark">VF</span>
        <span class="brand-text">
          <span class="brand-name">Video Factory</span>
          <span class="brand-sub">
            {#if health?.mock_only}mock mode{:else if health}live{/if}
            {#if integrations.length}
              · {integrations.filter(i => i.enabled).length}/{integrations.length} live
            {/if}
          </span>
        </span>
      </a>
    </div>

    <nav class="topnav" class:open={mobileNavOpen}>
      {#each navItems as item}
        <a href={item.href} onclick={() => mobileNavOpen = false}>{item.label}</a>
      {/each}
    </nav>

    <div class="topbar-right">
      <div class="workspace" onclick={(e) => { e.stopPropagation(); workspaceMenuOpen = !workspaceMenuOpen; }}>
        <span class="workspace-label">Workspace</span>
        <span class="workspace-name">{company?.name || 'Pick one'}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <path d="M2 3.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        </svg>
        {#if workspaceMenuOpen}
          <div class="workspace-menu" onclick={(e) => e.stopPropagation()}>
            {#each companies as c}
              <button class="ws-row {c.company_id === selectedId ? 'active' : ''}" onclick={() => setCompany(c)}>
                <span class="ws-row-name">{c.name}</span>
                <span class="ws-row-id">{c.company_id}</span>
              </button>
            {/each}
            <button class="ws-row ws-new" onclick={createCompany}>
              <span class="ws-row-name">+ New workspace</span>
            </button>
          </div>
        {/if}
      </div>

      <a href="/create" class="vf-btn vf-btn-primary new-job">+ New job</a>
    </div>
  </header>

  <main class="content">
    {#if company}
      {@render children()}
    {:else}
      <div class="empty-state">
        <h1>No workspace yet</h1>
        <p>Create a workspace to start producing branded videos.</p>
        <button class="vf-btn vf-btn-primary vf-btn-lg" onclick={createCompany}>+ Create workspace</button>
      </div>
    {/if}
  </main>
</div>

<style>
  .app {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  .topbar {
    position: sticky;
    top: 0;
    z-index: 50;
    display: flex;
    align-items: center;
    gap: var(--vf-sp-5);
    height: 64px;
    padding: 0 var(--vf-sp-7);
    background: rgba(10, 9, 9, 0.7);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--vf-border-subtle);
  }

  .topbar-left {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-3);
    flex-shrink: 0;
  }

  .vf-mobile-toggle {
    display: none;
    width: 36px;
    height: 36px;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    padding: 0;
    background: transparent;
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
  }
  .vf-mobile-toggle span {
    width: 16px;
    height: 1.5px;
    background: var(--vf-text-primary);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-3);
    color: inherit;
  }
  .brand-mark {
    position: relative;
    width: 36px;
    height: 36px;
    display: grid;
    place-items: center;
    font-weight: var(--vf-fw-bold);
    font-size: var(--vf-fs-ui);
    letter-spacing: 0.02em;
    background: var(--vf-bg);
    color: var(--vf-text-primary);
    border-radius: var(--vf-radius-md);
    background-clip: padding-box;
  }
  .brand-mark::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
    padding: 1.5px;
    background: var(--vf-grad-outline);
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor;
    mask-composite: exclude;
    pointer-events: none;
  }
  .brand-text {
    display: flex;
    flex-direction: column;
    line-height: 1.1;
  }
  .brand-name {
    font-weight: var(--vf-fw-semibold);
    font-size: var(--vf-fs-panels);
    letter-spacing: -0.01em;
  }
  .brand-sub {
    font-size: var(--vf-fs-cap-sm);
    color: var(--vf-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .topnav {
    display: flex;
    gap: var(--vf-sp-1);
    margin: 0 auto;
  }
  .topnav a {
    padding: 8px 14px;
    border-radius: var(--vf-radius-md);
    font-size: var(--vf-fs-default);
    font-weight: var(--vf-fw-medium);
    color: var(--vf-text-secondary);
    transition:
      background var(--vf-dur) var(--vf-ease),
      color var(--vf-dur) var(--vf-ease);
  }
  .topnav a:hover { background: var(--vf-surface-2); color: var(--vf-text-primary); }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-3);
    flex-shrink: 0;
  }

  .workspace {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--vf-sp-2);
    padding: 6px 12px;
    border-radius: var(--vf-radius-md);
    background: var(--vf-surface-1);
    border: 1px solid var(--vf-border);
    cursor: pointer;
    transition:
      background var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
  }
  .workspace:hover { background: var(--vf-surface-2); border-color: var(--vf-border-default); }
  .workspace-label {
    font-size: var(--vf-fs-cap-sm);
    color: var(--vf-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .workspace-name {
    font-size: var(--vf-fs-default);
    font-weight: var(--vf-fw-medium);
  }

  .workspace-menu {
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    min-width: 260px;
    background: var(--vf-surface-1);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    box-shadow: var(--vf-shadow-lg);
    padding: 6px;
    z-index: 60;
    animation: menuIn 200ms var(--vf-ease-out);
  }
  @keyframes menuIn {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .ws-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    width: 100%;
    text-align: left;
    background: transparent;
    border: 0;
    color: var(--vf-text-primary);
    padding: 8px 12px;
    border-radius: var(--vf-radius-sm);
    cursor: pointer;
    transition: background var(--vf-dur-fast) var(--vf-ease);
  }
  .ws-row:hover { background: var(--vf-surface-2); }
  .ws-row.active { background: var(--vf-surface-3); }
  .ws-row-name { font-weight: var(--vf-fw-medium); font-size: var(--vf-fs-default); }
  .ws-row-id { font-size: var(--vf-fs-cap-sm); color: var(--vf-text-secondary); font-family: var(--vf-font-mono); }
  .ws-new { color: var(--vf-butter-green); }

  .new-job { padding: 8px 16px; }

  .content {
    flex: 1;
    width: 100%;
    max-width: 1280px;
    margin: 0 auto;
    padding: var(--vf-sp-8) var(--vf-sp-7);
  }

  .empty-state {
    text-align: center;
    padding: 96px var(--vf-sp-7);
  }
  .empty-state h1 { margin-bottom: var(--vf-sp-3); }
  .empty-state p {
    color: var(--vf-text-secondary);
    margin-bottom: var(--vf-sp-6);
  }

  /* ─── Responsive ──────────────────────────────────────── */
  @media (max-width: 880px) {
    .topbar { padding: 0 var(--vf-sp-4); gap: var(--vf-sp-3); }
    .vf-mobile-toggle { display: inline-flex; }
    .topnav {
      position: absolute;
      top: 64px;
      left: 0;
      right: 0;
      flex-direction: column;
      align-items: stretch;
      gap: 0;
      padding: var(--vf-sp-3);
      background: var(--vf-surface-1);
      border-bottom: 1px solid var(--vf-border);
      transform: translateY(-8px);
      opacity: 0;
      pointer-events: none;
      transition:
        transform var(--vf-dur) var(--vf-ease),
        opacity var(--vf-dur) var(--vf-ease);
    }
    .topnav.open {
      transform: translateY(0);
      opacity: 1;
      pointer-events: auto;
    }
    .topnav a {
      padding: 12px 16px;
      font-size: var(--vf-fs-panels);
    }
    .workspace-label { display: none; }
    .new-job { display: none; }
    .brand-text { display: none; }
    .content { padding: var(--vf-sp-5) var(--vf-sp-4); }
  }

  @media (max-width: 480px) {
    .workspace-name { max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  }
</style>