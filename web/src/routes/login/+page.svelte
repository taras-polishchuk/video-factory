<script lang="ts">
	let { data, form } = $props<{
		data: { next: string };
		form?: { errors?: Record<string, string>; email?: string };
	}>();

	// Initialize from form result. Captured on first render; later
	// updates from a redirect+re-render will re-instantiate this
	// component (form action result). This matches the legacy
	// behavior of "echo server error back into the input".
	let email = $state(form?.email || "");
	let password = $state("");
	let submitting = $state(false);

	function onSubmit() {
		submitting = true;
	}
</script>

<svelte:head><title>Log in · Video Factory</title></svelte:head>

<div class="auth-page">
	<form class="auth-card" method="POST" onsubmit={onSubmit}>
		<h1>Welcome back</h1>
		<p class="sub">Sign in to your Video Factory workspace.</p>

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

		<label>
			<span>Password</span>
			<input
				type="password"
				name="password"
				autocomplete="current-password"
				required
				bind:value={password}
				aria-invalid={!!form?.errors?.password}
			/>
			{#if form?.errors?.password}
				<small class="field-error">{form.errors.password}</small>
			{/if}
		</label>

		<input type="hidden" name="next" value={data.next} />

		<button type="submit" class="vf-btn vf-btn-primary vf-btn-lg" disabled={submitting}>
			{submitting ? 'Signing in…' : 'Log in'}
		</button>

		<p class="alt">
			<a href="/forgot">Forgot password?</a>
			·
			<a href="/signup">Create an account</a>
		</p>
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
	.alt {
		margin: 0; font-size: var(--vf-fs-cap); color: var(--vf-text-secondary);
		text-align: center;
	}
	.alt a { color: var(--vf-butter-green); text-decoration: none; }
	.alt a:hover { text-decoration: underline; }
</style>
