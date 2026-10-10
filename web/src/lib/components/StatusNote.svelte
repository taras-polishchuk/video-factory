<script>
	// One inline state for async data: loading, error (with retry) or
	// empty. Pages render this instead of a blank card, a stuck
	// "Loading…" or a raw error string.
	let { kind = 'loading', title = '', message = '', onretry = undefined, children = undefined } = $props();
</script>

<div
	class="status {kind}"
	role={kind === 'error' ? 'alert' : 'status'}
	data-testid="vf-status-{kind}"
>
	{#if kind === 'loading'}<span class="spin" aria-hidden="true"></span>{/if}
	<div class="body">
		{#if title}<strong>{title}</strong>{/if}
		{#if message}<p>{message}</p>{/if}
		{@render children?.()}
	</div>
	{#if kind === 'error' && onretry}
		<button type="button" class="vf-btn vf-btn-ghost vf-btn-sm" onclick={onretry}>Try again</button>
	{/if}
</div>

<style>
	.status {
		display: flex; align-items: center; gap: var(--vf-sp-3);
		padding: var(--vf-sp-4);
		border-radius: var(--vf-radius-md);
		color: var(--vf-text-secondary);
	}
	.status .body { flex: 1; min-width: 0; }
	.status p { margin: 0; }
	.status strong { color: var(--vf-text-primary); display: block; }
	.status.error {
		background: var(--vf-error-surface);
		color: var(--vf-error-text);
		border: 1px solid rgba(248, 113, 113, 0.25);
	}
	.status.error strong { color: inherit; }
	.status.empty { justify-content: center; text-align: center; padding: var(--vf-sp-7); }
	.spin {
		width: 14px; height: 14px; flex: none;
		border: 2px solid currentColor; border-top-color: transparent;
		border-radius: 50%; animation: vf-status-spin 0.7s linear infinite;
	}
	@keyframes vf-status-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .spin { animation-duration: 2.4s; } }
</style>
