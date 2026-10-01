<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';

  let companies = $state([]);
  let company = $derived(companies[0]);
  let assets = $state([]);
  let uploading = $state(false);
  let err = $state('');

  $effect(() => {
    if (company) api.listAssets(company.company_id).then(a => assets = a);
  });

  onMount(async () => { companies = await api.listCompanies(); });

  async function upload(e) {
    const file = e.target.files[0];
    if (!file || !company) return;
    uploading = true; err = '';
    try {
      const a = await api.uploadAsset(company.company_id, 'asset', file);
      assets = [a, ...assets];
    } catch (e) { err = e.message; }
    finally { uploading = false; e.target.value = ''; }
  }
</script>

<svelte:head><title>Assets · Video Factory</title></svelte:head>

<h1>Assets</h1>
<p style="color: var(--text-dim); margin-top: 0;">Logos, fonts, examples, voice references. Uploaded files stay scoped to this workspace.</p>

<div class="card">
  <label>Upload asset</label>
  <input type="file" onchange={upload} disabled={uploading || !company} />
  {#if err}<div style="color: var(--err); margin-top: 8px;">{err}</div>{/if}
</div>

<div class="card">
  <h2>{assets.length} files</h2>
  {#if assets.length === 0}
    <p style="color: var(--text-dim);">No assets yet.</p>
  {:else}
    <table>
      <thead><tr><th>File</th><th>Kind</th><th>Size</th><th>Uploaded</th><th></th></tr></thead>
      <tbody>
        {#each assets as a}
          <tr>
            <td>{a.filename}</td>
            <td><span class="pill muted">{a.kind}</span></td>
            <td style="color: var(--text-dim);">{Math.round(a.size_bytes / 1024)} KB</td>
            <td style="color: var(--text-dim);">{a.uploaded_at}</td>
            <td><a href={api.assetDownloadUrl(a.asset_id)} style="color: var(--accent);">Download</a></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>