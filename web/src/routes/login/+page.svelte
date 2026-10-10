<script lang="ts">
	import { untrack } from 'svelte';

	let { data, form } = $props<{
		data: { next: string };
		form?: { errors?: Record<string, string>; email?: string };
	}>();

	// Plain (non-enhanced) form POST: a failed login re-renders this
	// page from the server with `form` set, so the email is seeded from
	// it ONCE on first render (untrack = intentional, no reactive link).
	// It stays a bound $state, not a `value={...}` attribute: Svelte
	// hydration would otherwise overwrite what the user already typed
	// while the JS bundle was still loading.
	let email = $state(untrack(() => form?.email ?? ''));
	let submitting = $state(false);

	function onSubmit() {
		submitting = true;
	}
</script>

<svelte:head><title>Log in · Video Factory</title></svelte:head>

<!-- bfcache can restore this page after "Back" with the button still
     disabled; reset it. -->
<svelte:window onpageshow={(e) => { if (e.persisted) submitting = false; }} />

<div class="auth-page">
	<!-- A-2: the route only has named actions, so the form must target
	     `?/login`. `next` rides along in the query (kept across a failed
	     attempt) and in the hidden field. -->
	<form
		class="auth-card"
		method="POST"
		action={`?/login&next=${encodeURIComponent(data.next)}`}
		onsubmit={onSubmit}
	>
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

			<div class="oidc-divider"><span>or</span></div>

			<a class="vf-btn vf-btn-oidc-google vf-btn-lg" href="/auth/oauth/google/start?redirect_after={encodeURIComponent('/')}">
				<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
					<path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.2-2.27H12v4.51h6.47c-.28 1.4-1.07 2.59-2.27 3.4v2.81h3.66c2.16-1.99 3.43-4.91 3.43-8.45z"/>
					<path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.66-2.81c-1.02.69-2.31 1.1-4.27 1.1-3.27 0-6.05-2.21-7.04-5.18H1.18v3.25A11.99 11.99 0 0 0 12 24z"/>
					<path fill="#FBBC04" d="M4.96 14.18A7.18 7.18 0 0 1 4.66 12c0-.76.13-1.5.3-2.18V6.57H1.18A11.99 11.99 0 0 0 0 12c0 1.94.46 3.77 1.18 5.43l3.78-3.25z"/>
					<path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.43-3.43C17.95 1.16 15.24 0 12 0A11.99 11.99 0 0 0 1.18 6.57l3.78 3.25C5.95 6.94 8.73 4.75 12 4.75z"/>
				</svg>
				Continue with Google
			</a>

			<a class="vf-btn vf-btn-oidc-github vf-btn-lg" href="/auth/oauth/github/start?redirect_after={encodeURIComponent('/')}">
				<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
					<path fill="currentColor" d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.1.79-.25.79-.56v-2.16c-3.2.7-3.87-1.36-3.87-1.36-.52-1.33-1.28-1.69-1.28-1.69-1.04-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.25.73-1.54-2.55-.29-5.23-1.28-5.23-5.7 0-1.26.45-2.29 1.18-3.1-.12-.29-.51-1.46.11-3.04 0 0 .96-.31 3.15 1.18a10.94 10.94 0 0 1 5.74 0c2.19-1.49 3.15-1.18 3.15-1.18.62 1.58.23 2.75.11 3.04.73.81 1.18 1.84 1.18 3.1 0 4.43-2.69 5.41-5.25 5.69.41.36.78 1.06.78 2.14v3.18c0 .31.21.67.8.56C20.21 21.39 23.5 17.08 23.5 12 23.5 5.65 18.35.5 12 .5z"/>
				</svg>
				Continue with GitHub
			</a>

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
	.oidc-divider {
		display: flex; align-items: center; gap: var(--vf-sp-3);
		color: var(--vf-text-secondary);
		font-size: var(--vf-fs-cap);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		margin: var(--vf-sp-2) 0;
	}
	.oidc-divider::before,
	.oidc-divider::after {
		content: ""; flex: 1; height: 1px;
		background: var(--vf-border);
	}
	.vf-btn-oidc-google,
	.vf-btn-oidc-github {
		display: inline-flex; align-items: center; justify-content: center;
		gap: var(--vf-sp-2);
		text-decoration: none;
		color: var(--vf-text-primary);
		background: var(--vf-bg);
		border: 1px solid var(--vf-border);
	}
	.vf-btn-oidc-google:hover,
	.vf-btn-oidc-github:hover {
		background: var(--vf-surface-2);
	}
</style>
