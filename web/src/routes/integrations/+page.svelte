<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  let items = $state([]);
  onMount(async () => { items = await api.integrations(); });
</script>

<svelte:head><title>Integrations · Video Factory</title></svelte:head>

<h1>Integrations</h1>
<p style="color: var(--text-dim); margin-top: 0;">Provider and channel integrations. Mock-only by default; live adapters stay disabled until a separately verified implementation is configured.</p>

<div class="card">
  <table>
    <thead><tr><th>Name</th><th>Mode</th><th>Status</th><th>Notes</th></tr></thead>
    <tbody>
      {#each items as i}
        <tr>
          <td><strong>{i.name}</strong></td>
          <td><span class="pill muted">{i.mode}</span></td>
          <td><span class="pill {i.enabled ? 'ok' : 'muted'}">{i.enabled ? 'connected' : 'disabled'}</span></td>
          <td style="color: var(--text-dim);">{i.detail}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>What's intentionally not connected</h2>
  <ul style="color: var(--text-dim); line-height: 1.8;">
    <li><strong>HeyGen direct</strong> — adapter builds a request body but unconditionally raises <code>ProviderDisabled</code>. Live POST gated by <code>LIVE_PROVIDER_TESTS=true</code> + a separately verified smoke test.</li>
    <li><strong>ComfyUI Cloud / GPU worker</strong> — contract-only. No HTTP call without the same gate.</li>
    <li><strong>ComfyUI HeyGen Partner Nodes</strong> — capability-gated via <code>/object_info</code>. No BYOK claim is made.</li>
    <li><strong>Publer</strong> — publishing is mocked locally; the API accepts draft/schedule/publish actions and records them in the audit log without making external calls.</li>
    <li><strong>n8n</strong> — workflow JSON template is provided as <code>workflows/n8n-trigger.json</code>. Live wiring requires a configured n8n instance.</li>
  </ul>
</div>