<script>
	import '../app.css';
	import WorkspaceSwitcher from '$lib/components/WorkspaceSwitcher.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { page } from '$app/stores';

	let { data, children } = $props();

	// Server-loaded auth state. SSR-first: no onMount, no fetch on
	// the client. The first paint already has these values.
	let user = $derived(data.user);
	let workspaces = $derived(data.workspaces || []);
	let active = $derived(data.activeWorkspace);
	let hasWorkspace = $derived(!!active);
	let showEmpty = $derived(hasWorkspace && !data.firstCredentialExists);

	// The /login, /signup, /forgot, /verify pages render their own
	// auth card and must not be wrapped in the app shell. Detect
	// them by pathname so the layout falls through to {@render
	// children()} for those routes.
	const PUBLIC_AUTH = ['/login', '/signup', '/forgot'];
	let isAuthPage = $derived(PUBLIC_AUTH.includes($page.url.pathname));
</script>

<div class="app-shell" class:auth-mode={isAuthPage}>
{#if !isAuthPage}
	<header class="topbar">
		<div class="topbar-left">
			<a href="/" class="brand">
				<span class="brand-mark">VF</span>
				<span class="brand-text">
					<span class="brand-name">Video Factory</span>
					<span class="brand-sub">control room</span>
				</span>
			</a>
		</div>

		<nav class="topnav">
			<a href="/">Overview</a>
			<a href="/bible">Video Bible</a>
			<a href="/assets">Assets</a>
			<a href="/jobs">Jobs</a>
			<a href="/create">Create</a>
			<a href="/integrations">Integrations</a>
			<a href="/credentials">Credentials</a>
		</nav>

		<div class="topbar-right">
			<WorkspaceSwitcher {workspaces} {active} />
			<span class="user-pill" title={user?.email}>{user?.email || ''}</span>
			<form method="POST" action="/logout" class="logout-form">
				<button type="submit" class="vf-btn vf-btn-ghost" aria-label="Log out">Log out</button>
			</form>
			<a href="/create" class="vf-btn vf-btn-primary new-job">+ New job</a>
		</div>
	</header>
{/if}

	<main class="content">
		{#if isAuthPage}
			{@render children()}
		{:else if !hasWorkspace}
			<EmptyState
				title="Welcome — paste your first provider key to get started"
				body="Create your first workspace to start producing branded videos."
				ctaHref="/credentials"
				ctaLabel="Open credentials"
			/>
		{:else if showEmpty}
			<EmptyState
				title="Welcome — paste your first provider key to get started"
				body="You're signed in. Add a provider key to start producing videos."
				ctaHref="/credentials"
				ctaLabel="Add provider key"
			/>
		{:else}
			{@render children()}
		{/if}
	</main>
</div>

<style>
	.app-shell { min-height: 100vh; display: flex; flex-direction: column; }
	.app-shell.auth-mode { display: block; }
	.app-shell.auth-mode .content { padding: 0; }

	.topbar {
		position: sticky; top: 0; z-index: 50;
		display: flex; align-items: center; gap: var(--vf-sp-5);
		height: 64px; padding: 0 var(--vf-sp-7);
		background: rgba(10, 9, 9, 0.7);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border-bottom: 1px solid var(--vf-border-subtle);
	}
	.topbar-left { display: flex; align-items: center; gap: var(--vf-sp-3); flex-shrink: 0; }

	.brand { display: flex; align-items: center; gap: var(--vf-sp-3); color: inherit; text-decoration: none; }
	.brand-mark {
		position: relative; width: 36px; height: 36px;
		display: grid; place-items: center;
		font-weight: var(--vf-fw-bold); font-size: var(--vf-fs-ui);
		background: var(--vf-bg); color: var(--vf-text-primary);
		border-radius: var(--vf-radius-md);
	}
	.brand-text { display: flex; flex-direction: column; line-height: 1.1; }
	.brand-name { font-weight: var(--vf-fw-semibold); font-size: var(--vf-fs-panels); }
	.brand-sub {
		font-size: var(--vf-fs-cap-sm);
		color: var(--vf-text-secondary);
		text-transform: uppercase; letter-spacing: 0.08em;
	}

	.topnav { display: flex; gap: var(--vf-sp-1); margin: 0 auto; }
	.topnav a {
		padding: 8px 14px; border-radius: var(--vf-radius-md);
		font-size: var(--vf-fs-default);
		color: var(--vf-text-secondary); text-decoration: none;
	}
	.topnav a:hover { background: var(--vf-surface-2); color: var(--vf-text-primary); }

	.topbar-right { display: flex; align-items: center; gap: var(--vf-sp-3); flex-shrink: 0; }

	.user-pill {
		font-size: var(--vf-fs-cap); color: var(--vf-text-secondary);
		padding: 4px 10px; border-radius: var(--vf-radius-pill);
		background: var(--vf-surface-1); border: 1px solid var(--vf-border);
	}
	.logout-form { margin: 0; }
	.logout-form button {
		padding: 6px 12px; font-size: var(--vf-fs-cap);
		background: transparent; color: var(--vf-text-secondary);
		border: 1px solid var(--vf-border); border-radius: var(--vf-radius-md);
		cursor: pointer;
	}
	.logout-form button:hover { color: var(--vf-text-primary); border-color: var(--vf-border-default); }

	.content {
		flex: 1; width: 100%; max-width: 1280px;
		margin: 0 auto; padding: var(--vf-sp-8) var(--vf-sp-7);
	}

	@media (max-width: 880px) {
		.topbar { padding: 0 var(--vf-sp-4); gap: var(--vf-sp-3); }
		.topnav { display: none; }
		.user-pill { display: none; }
		.content { padding: var(--vf-sp-5) var(--vf-sp-4); }
	}
</style>
