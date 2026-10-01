<script>
  import { api } from '$lib/api.js';
  import { onMount } from 'svelte';
  let items = $state([]);
  onMount(() => api.integrations().then(set => items = set));
</script>

<svelte:head><title>Integrations · Video Factory</title></svelte:head>

<header class="page-header">
  <div class="header-row">
    <div>
      <p class="eyebrow">Connections</p>
      <h1>Integrations</h1>
      <p class="sub">Provider and channel integrations. Live adapters are contract-only until a separately verified implementation is configured.</p>
    </div>
  </div>
</header>

<section class="vf-card">
  {#if items.length === 0}
    <p class="muted">Loading provider status…</p>
  {:else}
    <div class="provider-grid">
      {#each items as i}
        <article class="provider hoverable" class:enabled={i.enabled}>
          <header class="provider-head">
            <span class="provider-dot"></span>
            <h3>{i.name}</h3>
            <span class="vf-pill {i.enabled ? 'ok' : 'muted'}"><span class="dot"></span>{i.enabled ? 'connected' : 'disabled'}</span>
          </header>
          <p class="provider-mode">mode · <span class="mono">{i.mode}</span></p>
          <p class="provider-detail">{i.detail}</p>
        </article>
      {/each}
    </div>
  {/if}
</section>

<section class="vf-card vf-card-glow">
  <h2>What is intentionally not connected</h2>
  <ul class="explain">
    <li>
      <strong>HeyGen direct</strong> — adapter builds the request body but unconditionally raises <code>ProviderDisabled</code>. Live POST gated by <code>LIVE_PROVIDER_TESTS=true</code> + a separately verified smoke test.
    </li>
    <li>
      <strong>ComfyUI Cloud / GPU worker</strong> — contract-only. No HTTP call without the same gate. GPU bring-up is a Docker Compose that the operator runs separately.
    </li>
    <li>
      <strong>ComfyUI HeyGen Partner Nodes</strong> — capability-gated via <code>/object_info</code>. No BYOK claim is made; auth + billing belong to Comfy.
    </li>
    <li>
      <strong>Publer</strong> — publishing is mocked locally; the API accepts draft / schedule / publish actions and records them in the audit log without making external calls.
    </li>
    <li>
      <strong>n8n</strong> — workflow JSON template is provided as <code>workflows/n8n-trigger.json</code>. Live wiring requires a configured n8n instance.
    </li>
  </ul>
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

  .provider-grid {
    display: grid;
    gap: var(--vf-sp-4);
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }
  .provider {
    padding: var(--vf-sp-5);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    transition:
      transform var(--vf-dur) var(--vf-ease),
      border-color var(--vf-dur) var(--vf-ease);
  }
  .provider.hoverable:hover {
    transform: translateY(-2px);
    border-color: var(--vf-border-default);
  }
  .provider.enabled {
    background: var(--vf-success-surface);
    border-color: rgba(110, 232, 110, 0.25);
  }
  .provider-head {
    display: flex;
    align-items: center;
    gap: var(--vf-sp-2);
    margin-bottom: var(--vf-sp-3);
  }
  .provider-head h3 {
    font-size: var(--vf-fs-panels);
    font-weight: var(--vf-fw-medium);
    flex: 1;
    margin: 0;
    font-family: var(--vf-font-mono);
  }
  .provider-dot {
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: var(--vf-text-disabled);
    flex-shrink: 0;
  }
  .provider.enabled .provider-dot {
    background: var(--vf-success);
    box-shadow: 0 0 0 3px rgba(110, 232, 110, 0.18);
    animation: livePulse 2.5s ease-in-out infinite;
  }
  @keyframes livePulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(110, 232, 110, 0.4); }
    50% { box-shadow: 0 0 0 6px rgba(110, 232, 110, 0); }
  }
  .provider-mode {
    font-size: var(--vf-fs-cap-sm);
    color: var(--vf-text-secondary);
    margin: 0 0 var(--vf-sp-2);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .provider-detail {
    font-size: var(--vf-fs-ui);
    color: var(--vf-text-secondary);
    margin: 0;
    line-height: 1.5;
  }

  .explain {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--vf-sp-3);
  }
  .explain li {
    padding: var(--vf-sp-4);
    background: var(--vf-surface-2);
    border: 1px solid var(--vf-border);
    border-radius: var(--vf-radius-md);
    color: var(--vf-text-secondary);
    line-height: 1.6;
    font-size: var(--vf-fs-ui);
  }
  .explain li strong {
    color: var(--vf-text-primary);
    margin-right: 4px;
  }
  .explain li code {
    font-size: var(--vf-fs-cap);
    background: var(--vf-surface-3);
  }
</style>