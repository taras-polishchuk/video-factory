<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';

  let companies = $state([]);
  let company = $derived(companies.find(c => c.company_id === localStorage.getItem("vf.company_id")) || companies[0]);
  let assets = $state([]);
  let uploading = $state(false);
  let err = $state('');
  let dragOver = $state(false);

  $effect(() => {
    if (company) api.listAssets(company.company_id).then(a => assets = a);
  });

  onMount(async () => { companies = await api.listCompanies(); });

  async function doUpload(file) {
    if (!file || !company) return;
    uploading = true; err = '';
    try {
      const a = await api.uploadAsset(company.company_id, 'asset', file);
      assets = [a, ...assets];
    } catch (e) { err = e.message; }
    finally { uploading = false; }
  }

  function handleDrop(e) {
    e.preventDefault();
    dragOver = false;
    const f = e.dataTransfer.files?.[0];
    if (f) doUpload(f);
  }
</script>

<svelte:head><title>Assets · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">Brand files</p>
      <h1>Assets</h1>
      <p class="sub">Logos, fonts, voice references, and example videos. Files are scoped to the active workspace and never leave it.</p>
    </div>
    <div class="header-meta">
      <span class="vf-pill muted"><span class="dot"></span>{assets.length} on file</span>
    </div>
  </div>
</header>

<section class="vf-card">
  <label
    class="dropzone"
    class:dragging={dragOver}
    ondragover={(e) => { e.preventDefault(); dragOver = true; }}
    ondragleave={() => dragOver = false}
    ondrop={handleDrop}
  >
    <input
      type="file"
      disabled={uploading || !company}
      onchange={(e) => {
        const inputEl = e.currentTarget;
        const f = inputEl.files?.[0];
        if (f) doUpload(f);
        inputEl.value = '';
      }}
      hidden
    />
    <div class="dz-icon">↑</div>
    <div class="dz-text">
      <strong>Drop a file</strong> or <span class="link">browse</span>
    </div>
    <div class="dz-meta">PNG, JPG, MP4, WAV, TTF · up to 50 MB</div>
  </label>
  {#if err}<div class="alert error">{err}</div>{/if}
</section>

<section class="vf-card">
  <header class="card-header">
    <div>
      <h2>All assets</h2>
      <p class="muted">Files scoped to this workspace. Delete is not exposed in V0.</p>
    </div>
  </header>

  {#if assets.length === 0}
    <div class="empty">
      <p>No assets yet.</p>
      <p class="muted">Upload a logo or an example video to get going.</p>
    </div>
  {:else}
    <div class="asset-grid">
      {#each assets as a}
        <a href={api.assetDownloadUrl(a.asset_id)} class="asset hoverable" download={a.filename}>
          <div class="asset-thumb">
            <span class="asset-ext">{a.filename.split('.').pop()?.toUpperCase()}</span>
          </div>
          <div class="asset-meta">
            <span class="asset-name">{a.filename}</span>
            <span class="muted">{Math.round(a.size_bytes / 1024)} KB</span>
            <span class="muted mono">{a.uploaded_at}</span>
          </div>
        </a>
      {/each}
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

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--vf-sp-3);
    margin-bottom: var(--vf-sp-5);
  }

  .dropzone {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--vf-sp-2);
    padding: var(--vf-sp-9) var(--vf-sp-5);
    background: var(--vf-surface-2);
    border: 2px dashed var(--vf-border-default);
    border-radius: var(--vf-radius-md);
    text-align: center;
    cursor: pointer;
    transition:
      border-color var(--vf-dur) var(--vf-ease),
      background var(--vf-dur) var(--vf-ease);
  }
  .dropzone:hover, .dropzone.dragging {
    border-color: var(--vf-butter-green);
    background: var(--vf-surface-3);
  }
  .dz-icon {
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: var(--vf-surface-3);
    border: 1px solid var(--vf-border);
    display: grid;
    place-items: center;
    font-size: 24px;
    color: var(--vf-butter-green);
    margin-bottom: var(--vf-sp-2);
  }
  .dz-text strong { font-weight: var(--vf-fw-semibold); }
  .link { color: var(--vf-butter-green); }
  .dz-meta { font-size: var(--vf-fs-cap); color: var(--vf-text-secondary); }

  .alert.error {
    background: var(--vf-error-surface);
    color: var(--vf-error-text);
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: var(--vf-radius-md);
    padding: var(--vf-sp-3);
    margin-top: var(--vf-sp-4);
    font-size: var(--vf-fs-ui);
  }

  .asset-grid {
    display: grid;
    gap: var(--vf-sp-3);
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  }
  .asset {
    text-decoration: none;
    color: inherit;
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    overflow: hidden;
    transition:
      transform var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
  }
  .asset.hoverable:hover {
    transform: translateY(-2px);
    border-color: var(--vf-butter-green);
  }
  .asset-thumb {
    aspect-ratio: 16 / 9;
    background:
      linear-gradient(135deg, rgba(110,232,110,0.05), rgba(255,216,77,0.05)),
      var(--vf-surface-3);
    display: grid;
    place-items: center;
    border-bottom: 1px solid var(--vf-border);
  }
  .asset-ext {
    font-family: var(--vf-font-mono);
    font-size: var(--vf-fs-titles);
    font-weight: var(--vf-fw-medium);
    color: var(--vf-text-secondary);
    letter-spacing: 0.05em;
  }
  .asset-meta {
    padding: var(--vf-sp-3);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .asset-name {
    font-size: var(--vf-fs-default);
    font-weight: var(--vf-fw-medium);
    color: var(--vf-text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .empty {
    text-align: center;
    padding: var(--vf-sp-7);
    color: var(--vf-text-secondary);
  }
</style>