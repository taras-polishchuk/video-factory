<script lang="ts">
	let { form } = $props<{
		form?: { ok?: boolean; email?: string; errors?: Record<string, string> };
	}>();

	let email = $state(form?.email || "");
	let submitting = $state(false);

	function onSubmit() { submitting = true; }
</script>

<svelte:head><title>Reset password · Video Factory</title></svelte:head>

<div class="auth-page">
	<form class="auth-card" method="POST" onsubmit={onSubmit}>
		{#if form?.ok}
			<h1>Check your inbox</h1>
			<p class="sub">
				If an account exists for <strong>{form.email}</strong>, we just sent a
				magic link to it. The link is good for one hour.
			</p>
			<a class="vf-btn vf-btn-ghost vf-btn-lg" href="/login">Back to log in</a>
		{:else}
			<h1>Forgot password</h1>
			<p class="sub">We'll email you a one-time magic link.</p>

			{#if form?.errors?._form}
				<div class="alert error">{form.errors._form}</div>
			{/if}

			<label>
				<span>Email</span>
				<input
					type="email"
					name="email"
					autocomplete="email"
					required
					bind:value={email}
					aria-invalid={!!form?.errors?.email}
				/>
				{#if form?.errors?.email}
					<small class="field-error">{form.errors.email}</small>
				{/if}
			</label>

			<button type="submit" class="vf-btn vf-btn-primary vf-btn-lg" disabled={submitting}>
				{submitting ? 'Sending…' : 'Send magic link'}
			</button>

			<p class="alt">
				<a href="/login">Back to log in</a>
			</p>
		{/if}
	</form>
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
	.auth-card label { display: flex; flex-direction: column; gap: 6px; }
	.auth-card label span {
		font-size: var(--vf-fs-cap); color: var(--vf-text-secondary);
		text-transform: uppercase; letter-spacing: 0.06em;
	}
	.auth-card input {
		font: inherit; padding: 10px 12px;
		background: var(--vf-bg); color: var(--vf-text-primary);
		border: 1px solid var(--vf-border);
		border-radius: var(--vf-radius-md);
	}
	.auth-card input:focus { outline: 2px solid var(--vf-butter-green); outline-offset: 1px; }
	.field-error { color: var(--vf-error-text); font-size: var(--vf-fs-cap); }
	.alert {
		padding: 10px 12px; border-radius: var(--vf-radius-md);
		font-size: var(--vf-fs-default);
	}
	.alert.error { background: var(--vf-error-surface); color: var(--vf-error-text); }
	.alt { margin: 0; font-size: var(--vf-fs-cap); color: var(--vf-text-secondary); text-align: center; }
	.alt a { color: var(--vf-butter-green); text-decoration: none; }
	.alt a:hover { text-decoration: underline; }
</style>
