<script>
  let { data, form } = $props();

  let workspace = $derived(data.activeWorkspace || null);
  let plan = $derived(workspace?.plan || 'free');
  let paid = $derived(plan === 'pro' || plan === 'enterprise');
  // Unknown (null) counts as off: no button leads to an error.
  let billingOn = $derived(data.billingEnabled === true);
</script>

<svelte:head><title>Billing · Video Factory</title></svelte:head>

<header class="page-header">
  <p class="eyebrow">Billing</p>
  <h1>Plan & subscription</h1>
  {#if workspace}
    <p class="sub">
      Workspace <strong>{workspace.name}</strong> is on the <strong>{plan}</strong> plan.
    </p>
  {/if}
</header>

{#if form?.message}
  <div class="flash error" role="alert" data-testid="billing-error">{form.message}</div>
{/if}

<section class="vf-card">
  {#if !workspace}
    <p class="muted">Select a workspace to manage billing.</p>
  {:else}
    {#if !billingOn}
      <p class="flash error" role="status" data-testid="billing-off">
        {#if data.billingEnabled === false}
          Billing is not set up on this deployment yet. Ask the operator to configure it.
        {:else}
          Billing status is unavailable right now. Try again in a moment.
        {/if}
      </p>
    {/if}

    <div class="actions">
      {#if !paid}
        <form method="POST" action="?/checkout">
          <button type="submit" class="vf-btn vf-btn-primary" disabled={!billingOn}>
            Upgrade to Pro
          </button>
        </form>
      {/if}
      <form method="POST" action="?/portal">
        <button type="submit" class="vf-btn vf-btn-ghost" disabled={!billingOn || !paid}>
          Manage subscription
        </button>
      </form>
    </div>
    {#if billingOn && !paid}
      <p class="muted small">Manage subscription opens once the workspace has a paid plan.</p>
    {/if}
  {/if}
</section>

<style>
  .actions {
    display: flex;
    gap: var(--vf-sp-3, 12px);
    flex-wrap: wrap;
    margin-top: var(--vf-sp-4, 16px);
  }
  .flash {
    margin-bottom: var(--vf-sp-3, 12px);
    padding: var(--vf-sp-3, 12px) var(--vf-sp-4, 16px);
    border-radius: 8px;
  }
  .flash.error {
    border: 1px solid var(--vf-danger, #b91c1c);
    color: var(--vf-danger, #b91c1c);
  }
  .muted {
    color: var(--vf-text-secondary);
  }
  .small {
    font-size: 0.85em;
  }
</style>
