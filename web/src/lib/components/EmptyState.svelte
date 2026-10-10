<script lang="ts">
	// Onboarding / empty block. `compact` renders it inline above other
	// content (overview); the default is the centred full-page variant.
	// CTA is optional so a state with nothing to click (e.g. "no
	// workspace") does not render a dead link.
	let {
		title,
		body,
		ctaHref = '',
		ctaLabel = '',
		hint = '',
		secondaryHref = '',
		secondaryLabel = '',
		compact = false
	} = $props<{
		title: string;
		body: string;
		ctaHref?: string;
		ctaLabel?: string;
		hint?: string;
		secondaryHref?: string;
		secondaryLabel?: string;
		compact?: boolean;
	}>();
</script>

<section class="empty-state" class:compact data-testid="vf-empty-state">
	<h2 class="title">{title}</h2>
	<p class="body">{body}</p>
	{#if ctaHref && ctaLabel}
		<div class="actions">
			<a class="vf-btn vf-btn-primary vf-btn-lg" href={ctaHref}>{ctaLabel}</a>
			{#if secondaryHref && secondaryLabel}
				<a class="vf-btn vf-btn-ghost vf-btn-lg" href={secondaryHref}>{secondaryLabel}</a>
			{/if}
		</div>
	{/if}
	{#if hint}<p class="hint">{hint}</p>{/if}
</section>

<style>
	.empty-state {
		text-align: center;
		padding: 96px var(--vf-sp-7);
		max-width: 560px;
		margin: 0 auto;
	}
	.empty-state.compact {
		max-width: none;
		padding: var(--vf-sp-7) var(--vf-sp-6);
		margin: 0 0 var(--vf-sp-7);
		background: var(--vf-surface-1);
		border: 1px solid var(--vf-border);
		border-radius: var(--vf-radius-lg);
	}
	.title { margin: 0 0 var(--vf-sp-3); font-size: var(--vf-fs-h2, 1.5rem); }
	.body {
		color: var(--vf-text-secondary);
		margin: 0 auto var(--vf-sp-6);
		max-width: 52ch;
	}
	.actions { display: flex; justify-content: center; gap: var(--vf-sp-3); flex-wrap: wrap; }
	.hint {
		color: var(--vf-text-secondary);
		font-size: var(--vf-fs-cap);
		margin: var(--vf-sp-5) auto 0;
		max-width: 56ch;
	}
</style>
