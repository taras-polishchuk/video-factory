<script lang="ts">
	let { form } = $props<{
		form?: {
			errors?: Record<string, string>;
			email?: string;
			workspace_name?: string;
		};
	}>();

	let email = $state(form?.email || "");
	let password = $state("");
	let workspace_name = $state(form?.workspace_name || "");
	let submitting = $state(false);

	function onSubmit() {
		submitting = true;
	}
</script>

<svelte:head><title>Create an account · Video Factory</title></svelte:head>

<div class="auth-page">
	<form class="auth-card" method="POST" onsubmit={onSubmit}>
		<h1>Create your account</h1>
		<p class="sub">30 seconds to your first branded video.</p>

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
				autocomplete="new-password"
				minlength="8"
				required
				bind:value={password}
				aria-invalid={!!form?.errors?.password}
			/>
			{#if form?.errors?.password}
				<small class="field-error">{form.errors.password}</small>
			{:else}
				<small class="hint">At least 8 characters</small>
			{/if}
		</label>

		<label>
			<span>Workspace name</span>
			<input
				type="text"
				name="workspace_name"
				required
				maxlength="80"
				bind:value={workspace_name}
				aria-invalid={!!form?.errors?.workspace_name}
			/>
			{#if form?.errors?.workspace_name}
				<small class="field-error">{form.errors.workspace_name}</small>
			{:else}
				<small class="hint">The company or team this workspace produces videos for</small>
			{/if}
		</label>

		<button type="submit" class="vf-btn vf-btn-primary vf-btn-lg" disabled={submitting}>
			{submitting ? 'Creating…' : 'Create account'}
		</button>

		<p class="alt">
			Already have an account? <a href="/login">Log in</a>
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
		width: 100%; max-width: 460px;
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
	.hint { color: var(--vf-text-disabled); font-size: var(--vf-fs-cap); }
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
