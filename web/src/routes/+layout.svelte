<script>
	import '../app.css';
	import WorkspaceSwitcher from '$lib/components/WorkspaceSwitcher.svelte';
	import { page } from '$app/stores';
	import { afterNavigate } from '$app/navigation';

	let { data, children } = $props();

	// Server-loaded auth state. SSR-first: no onMount, no fetch on
	// the client. The first paint already has these values.
	let user = $derived(data.user);
	let workspaces = $derived(data.workspaces || []);
	let active = $derived(data.activeWorkspace);

	// NOTE (A-1/A-4): this layout NEVER replaces `children` with an
	// onboarding screen. The "paste your first provider key" welcome
	// lives on the overview page only (routes/+page.svelte) so the
	// credentials form and every other route stay reachable.

	// The /login, /signup, /forgot, /verify pages render their own
	// auth card and must not be wrapped in the app shell. Detect
	// them by pathname so the layout falls through to {@render
	// children()} for those routes.
	const PUBLIC_AUTH = ['/login', '/signup', '/forgot'];
	let isAuthPage = $derived(PUBLIC_AUTH.includes($page.url.pathname));

	const NAV = [
		{ href: '/', label: 'Overview' },
		{ href: '/bible', label: 'Video Bible' },
		{ href: '/assets', label: 'Assets' },
		{ href: '/jobs', label: 'Jobs' },
		{ href: '/create', label: 'Create' },
		{ href: '/integrations', label: 'Integrations' },
		{ href: '/credentials', label: 'Credentials' }
	];

	function isCurrent(href) {
		const path = $page.url.pathname;
		return href === '/' ? path === '/' : path === href || path.startsWith(href + '/');
	}

	// Narrow-screen (<= 1100px, so every phone) navigation: the inline
	// top nav is replaced by a disclosure menu. The button owns aria-expanded; Escape, a tap
	// outside the header, and any navigation close it.
	let menuOpen = $state(false);
	let menuButton = $state();
	let headerEl = $state();

	afterNavigate(() => {
		menuOpen = false;
	});

	function onWindowKeydown(e) {
		if (e.key === 'Escape' && menuOpen) {
			menuOpen = false;
			menuButton?.focus();
		}
	}

	function onWindowClick(e) {
		// composedPath() is captured at dispatch time. e.target is not
		// reliable here: the toggle swaps its icon, so by the time this
		// window listener runs the clicked <path> is already detached.
		if (menuOpen && headerEl && !e.composedPath().includes(headerEl)) {
			menuOpen = false;
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onclick={onWindowClick} />

<div class="app-shell" class:auth-mode={isAuthPage}>
{#if !isAuthPage}
	<a class="skip-link" href="#main">Skip to content</a>
	<header class="topbar" bind:this={headerEl}>
		<div class="topbar-left">
			<a href="/" class="brand">
				<span class="brand-mark">VF</span>
				<span class="brand-text">
					<span class="brand-name">Video Factory</span>
					<span class="brand-sub">control room</span>
				</span>
			</a>
		</div>

		<button
			type="button"
			class="menu-toggle"
			bind:this={menuButton}
			aria-expanded={menuOpen}
			aria-controls="topbar-panel"
			aria-label="Main menu"
			onclick={() => (menuOpen = !menuOpen)}
		>
			<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
				{#if menuOpen}
					<path d="M4 4l12 12M16 4L4 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
				{:else}
					<path d="M3 5.5h14M3 10h14M3 14.5h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
				{/if}
			</svg>
		</button>

		<div class="topbar-panel" class:open={menuOpen} id="topbar-panel">
			<nav class="topnav" aria-label="Primary">
				{#each NAV as item}
					<a href={item.href} aria-current={isCurrent(item.href) ? 'page' : undefined}>{item.label}</a>
				{/each}
			</nav>

			<div class="topbar-right">
				<WorkspaceSwitcher {workspaces} {active} />
				<span class="user-pill" title={user?.email}>{user?.email || ''}</span>
				<form method="POST" action="/logout" class="logout-form">
					<button type="submit" class="vf-btn vf-btn-ghost">Log out</button>
				</form>
				<a href="/create" class="vf-btn vf-btn-primary new-job">+ New job</a>
			</div>
		</div>
	</header>
{/if}

	<main class="content" id="main" tabindex="-1">
		{@render children()}
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

	/* Desktop: the panel is transparent to layout, so nav + account
	   controls sit directly in the topbar flex row. */
	.topbar-panel { display: contents; }
	.menu-toggle { display: none; }

	.topnav { display: flex; gap: var(--vf-sp-1); margin: 0 auto; }
	.topnav a {
		padding: 8px 14px; border-radius: var(--vf-radius-md);
		font-size: var(--vf-fs-default); white-space: nowrap;
		color: var(--vf-text-secondary); text-decoration: none;
	}
	.topnav a:hover { background: var(--vf-surface-2); color: var(--vf-text-primary); }
	.topnav a[aria-current='page'] { color: var(--vf-text-primary); background: var(--vf-surface-2); }

	.topbar-right { display: flex; align-items: center; gap: var(--vf-sp-3); flex-shrink: 1; min-width: 0; }

	/* A-7: long emails truncate with an ellipsis instead of clipping. */
	.user-pill {
		font-size: var(--vf-fs-cap); color: var(--vf-text-secondary);
		padding: 4px 10px; border-radius: var(--vf-radius-pill);
		background: var(--vf-surface-1); border: 1px solid var(--vf-border);
		display: inline-block; max-width: 180px; min-width: 0;
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.logout-form { margin: 0; }
	.new-job { white-space: nowrap; }

	.skip-link {
		position: absolute; left: var(--vf-sp-4); top: -48px; z-index: 100;
		padding: 8px 14px; border-radius: var(--vf-radius-md);
		background: var(--vf-surface-3); color: var(--vf-text-primary);
		border: 1px solid var(--vf-border-default);
	}
	.skip-link:focus { top: var(--vf-sp-3); }

	.topnav a:focus-visible, .brand:focus-visible, .menu-toggle:focus-visible {
		outline: 2px solid var(--vf-butter-green); outline-offset: 2px;
	}
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
	.content:focus { outline: none; }

	/* Compact desktop band: tighten the topbar before it runs out of
	   room (A-7). The inline nav + account controls need ~1200px. */
	@media (max-width: 1400px) {
		.topbar { padding: 0 var(--vf-sp-5); gap: var(--vf-sp-3); }
		.topnav { gap: 0; }
		.topnav a { padding: 8px 9px; }
		.brand-sub { display: none; }
		.user-pill { max-width: 110px; }
		.topbar-right { gap: var(--vf-sp-2); }
	}

	/* A-6: <= 1100px the inline nav no longer fits, so it becomes a
	   disclosure menu (this also covers every phone width, <= 880px). */
	@media (max-width: 1100px) {
		.topbar { padding: 0 var(--vf-sp-4); gap: var(--vf-sp-3); justify-content: space-between; }
		.menu-toggle {
			display: inline-grid; place-items: center;
			width: 44px; height: 44px; padding: 0;
			background: transparent; color: var(--vf-text-primary);
			border: 1px solid var(--vf-border); border-radius: var(--vf-radius-md);
			cursor: pointer;
		}
		.menu-toggle:hover { background: var(--vf-surface-2); }

		.topbar-panel {
			display: none;
			position: absolute; top: 100%; left: 0; right: 0;
			flex-direction: column; gap: var(--vf-sp-4);
			padding: var(--vf-sp-4);
			max-height: calc(100vh - 64px); overflow-y: auto;
			background: var(--vf-surface-1);
			border-bottom: 1px solid var(--vf-border);
			box-shadow: var(--vf-shadow-lg);
		}
		.topbar-panel.open { display: flex; }

		.topnav { flex-direction: column; margin: 0; gap: 2px; }
		.topnav a { padding: 12px 14px; font-size: var(--vf-fs-ui); }

		.topbar-right {
			flex-direction: column; align-items: stretch; gap: var(--vf-sp-3);
			padding-top: var(--vf-sp-4); border-top: 1px solid var(--vf-border-subtle);
		}
		.user-pill { max-width: 100%; text-align: center; }
		.logout-form button, .new-job { width: 100%; justify-content: center; min-height: 44px; }
	}

	@media (max-width: 880px) {
		.content { padding: var(--vf-sp-5) var(--vf-sp-4); }
	}
</style>
