<script lang="ts">
	let { data, form } = $props<{
		data: { token: string; missing: boolean };
		form?: { ok?: boolean; expired?: boolean; errors?: Record<string, string> };
	}>();

	let new_password = $state("");
	let confirm_password = $state("");
	let submitting = $state(false);

	function onSubmit() { submitting = true; }
</script>

<svelte:head>
	<title>Choose a new password · Video Factory</title>
	<!-- The reset token is in this page's URL: never send it as Referer
	     to another origin. "same-origin", not "no-referrer": with
	     no-referrer the browser sends `Origin: null` on the form POST
	     and the S1b Origin check (correctly) refuses it. -->
	<meta name="referrer" content="same-origin" />
</svelte:head>

<div class="auth-page">
	{#if form?.ok}
		<div class="auth-card">
			<h1>Password updated</h1>
			<p class="sub">Your password was changed and every open session was signed out. Log in with the new password.</p>
			<a class="vf-btn vf-btn-primary vf-btn-lg" href="/login">Log in</a>
		</div>
	{:else if data.missing || form?.expired}
		<div class="auth-card">
			<h1>Link not valid</h1>
			<p class="sub">
				{form?.errors?._form || 'This reset link is missing or incomplete. Open the link from the email again, or ask for a new one.'}
			</p>
			<a class="vf-btn vf-btn-primary vf-btn-lg" href="/forgot">Ask for a new link</a>
			<p class="alt"><a href="/login">Back to log in</a></p>
		</div>
	{:else}
		<form class="auth-card" method="POST" onsubmit={onSubmit}>
			<h1>Choose a new password</h1>
			<p class="sub">At least 12 characters with upper-case, lower-case letters and a digit.</p>

			{#if form?.errors?._form}
				<div class="alert error">{form.errors._form}</div>
			{/if}

			<input type="hidden" name="token" value={data.token} />

			<label>
				<span>New password</span>
				<input
					type="password"
					name="new_password"
					autocomplete="new-password"
					required
					minlength="12"
					bind:value={new_password}
					aria-invalid={!!form?.errors?.new_password}
				/>
				{#if form?.errors?.new_password}
					<small class="field-error">{form.errors.new_password}</small>
				{/if}
			</label>

			<label>
				<span>Repeat new password</span>
				<input
					type="password"
					name="confirm_password"
					autocomplete="new-password"
					required
					bind:value={confirm_password}
					aria-invalid={!!form?.errors?.confirm_password}
				/>
				{#if form?.errors?.confirm_password}
					<small class="field-error">{form.errors.confirm_password}</small>
				{/if}
			</label>

			<button type="submit" class="vf-btn vf-btn-primary vf-btn-lg" disabled={submitting}>
				{submitting ? 'Saving…' : 'Set new password'}
			</button>

			<p class="alt"><a href="/login">Back to log in</a></p>
		</form>
	{/if}
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
