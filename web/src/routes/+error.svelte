<script>
	import { page } from '$app/stores';

	// Friendly error screen for every unhandled load/render failure
	// (replaces the bare "500" heading). It renders inside the app
	// shell, so the nav stays usable.
	let status = $derived($page.status);
	let title = $derived(
		status === 404 ? 'Page not found'
		: status === 401 ? 'Please log in'
		: status === 403 ? 'You don’t have access to this'
		: status >= 500 ? 'Something went wrong on our side'
		: 'We couldn’t complete that request'
	);
	// Only 4xx messages are meant for people; never echo a 5xx detail.
	let detail = $derived(
		status >= 500
			? 'The problem has been noted. Try again in a moment; if it keeps happening, contact support.'
			: ($page.error && $page.error.message) || ''
	);
</script>

<svelte:head><title>{status} · Video Factory</title></svelte:head>

<section class="vf-card error-page" role="alert" data-testid="vf-error-page">
	<p class="code">{status}</p>
	<h1>{title}</h1>
	{#if detail}<p class="detail">{detail}</p>{/if}
	<div class="actions">
		<a class="vf-btn vf-btn-primary" href="/">Back to overview</a>
		{#if status === 401}
			<a class="vf-btn vf-btn-ghost" href="/login">Log in</a>
		{:else}
			<a class="vf-btn vf-btn-ghost" href="/jobs">Go to jobs</a>
		{/if}
	</div>
</section>

<style>
	.error-page { max-width: 560px; margin: var(--vf-sp-8) auto; text-align: center; }
	.code {
		margin: 0 0 var(--vf-sp-2);
		font-family: var(--vf-font-mono);
		font-size: var(--vf-fs-cap);
		letter-spacing: 0.08em;
		color: var(--vf-text-secondary);
	}
	h1 { margin: 0 0 var(--vf-sp-3); }
	.detail { color: var(--vf-text-secondary); margin: 0 auto var(--vf-sp-6); max-width: 46ch; }
	.actions { display: flex; justify-content: center; gap: var(--vf-sp-3); flex-wrap: wrap; }
</style>
