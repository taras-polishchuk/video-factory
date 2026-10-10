<script lang="ts">
	let { data } = $props<{ data: { ok: boolean; email_verified: boolean } }>();
</script>

<svelte:head>
	<title>Email verified · Video Factory</title>
	<!-- The verification token is in this page's URL: never send it as
	     Referer to another origin (same policy as /reset). -->
	<meta name="referrer" content="same-origin" />
</svelte:head>

<div class="auth-page">
	<div class="auth-card">
		{#if data.ok && data.email_verified}
			<h1>Email verified</h1>
			<p class="sub">You're all set. Sign in to start producing.</p>
			<a class="vf-btn vf-btn-primary vf-btn-lg" href="/login">Log in</a>
		{:else}
			<h1>Verification pending</h1>
			<p class="sub">Open the link from your email to verify your account.</p>
		{/if}
	</div>
</div>

<style>
	.auth-page {
		min-height: 100vh;
		display: grid; place-items: center;
		padding: var(--vf-sp-7);
	}
	.auth-card {
		width: 100%; max-width: 420px;
		display: flex; flex-direction: column; gap: var(--vf-sp-4);
		padding: var(--vf-sp-7);
		background: var(--vf-surface-1);
		border: 1px solid var(--vf-border);
		border-radius: var(--vf-radius-lg);
		box-shadow: var(--vf-shadow-lg);
	}
	.auth-card h1 { margin: 0; }
	.auth-card .sub { color: var(--vf-text-secondary); margin: 0; }
</style>
