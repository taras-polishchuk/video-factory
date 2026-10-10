<script lang="ts">
	import type { AuthWorkspace } from '$lib/server/types';
	import { page } from '$app/stores';

	let { workspaces, active } = $props<{
		workspaces: AuthWorkspace[];
		active: AuthWorkspace | null;
	}>();

	// The switcher is a native <details>/<form> combo. Zero
	// client-side JavaScript: the menu opens on click via the
	// HTML <details> element, and switching is a plain POST form
	// that the +page.server.ts handles.
	let open = $state(false);
	// After a switch the action redirects back here, not to the login page.
	let back = $derived($page.url.pathname + $page.url.search);
	let switching = $state<string | null>(null);

	function pick(id: string) {
		switching = id;
		// The form below submits immediately when its button is
		// clicked, so this only runs while the request is in flight.
		setTimeout(() => { switching = null; }, 4000);
	}
</script>

{#if workspaces.length > 1}
	<details class="ws" bind:open>
		<summary>
			<span class="ws-label">Workspace</span>
			<span class="ws-name">{active?.name || 'Pick one'}</span>
			<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
				<path d="M2 3.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
			</svg>
		</summary>
		<div class="ws-menu">
			{#each workspaces as w}
				<form method="POST" action={`/login?/switch&back=${encodeURIComponent(back)}`} class="ws-form">
					<input type="hidden" name="workspace_id" value={w.workspace_id} />
					<button
						type="submit"
						class="ws-row {w.workspace_id === active?.workspace_id ? 'active' : ''}"
						onclick={() => pick(w.workspace_id)}
					>
						<span class="ws-row-name">{w.name}</span>
						<span class="ws-row-id">{w.role} · {w.workspace_id}</span>
					</button>
				</form>
			{/each}
		</div>
	</details>
{:else if active}
	<span class="ws-static" title={active.slug}>
		<span class="ws-label">Workspace</span>
		<span class="ws-name">{active.name}</span>
	</span>
{/if}

<style>
	.ws, .ws-static {
		position: relative;
		display: inline-flex; align-items: center;
		gap: var(--vf-sp-2);
		padding: 6px 12px;
		border-radius: var(--vf-radius-md);
		background: var(--vf-surface-1);
		border: 1px solid var(--vf-border);
	}
	.ws-static .ws-label { display: inline; }
	.ws, .ws-static { max-width: 100%; min-width: 0; white-space: nowrap; }

	.ws > summary {
		display: inline-flex; align-items: center; gap: var(--vf-sp-2);
		list-style: none; cursor: pointer; user-select: none;
	}
	.ws > summary::-webkit-details-marker { display: none; }

	.ws-label {
		font-size: var(--vf-fs-cap-sm);
		color: var(--vf-text-secondary);
		text-transform: uppercase; letter-spacing: 0.06em;
	}
	.ws-name {
		font-size: var(--vf-fs-default); font-weight: var(--vf-fw-medium);
		max-width: 160px; overflow: hidden; text-overflow: ellipsis;
	}

	/* The "WORKSPACE" caption is the first thing to go when the topbar
	   gets tight; it comes back inside the mobile menu. */
	/* Inside the mobile menu the switcher is a full-width block and its
	   list opens inline instead of floating over the page. */
	@media (max-width: 1100px) {
		.ws, .ws-static { display: flex; width: 100%; }
		.ws > summary { width: 100%; }
		.ws-name { max-width: none; flex: 1; }
		.ws-menu { position: static; min-width: 0; margin-top: var(--vf-sp-2); box-shadow: none; }
	}
	@media (max-width: 1400px) and (min-width: 1101px) {
		.ws-label { display: none !important; }
		.ws-name { max-width: 110px; }
	}

	.ws-menu {
		position: absolute; top: calc(100% + 8px); right: 0;
		min-width: 260px; padding: 6px;
		background: var(--vf-surface-1);
		border: 1px solid var(--vf-border);
		border-radius: var(--vf-radius-md);
		box-shadow: var(--vf-shadow-lg);
		z-index: 60;
	}
	.ws-form { margin: 0; padding: 0; }
	.ws-row {
		display: flex; flex-direction: column; align-items: flex-start;
		width: 100%; padding: 8px 12px;
		background: transparent; color: var(--vf-text-primary);
		border: 0; border-radius: var(--vf-radius-sm);
		font: inherit; text-align: left; cursor: pointer;
	}
	.ws-row:hover { background: var(--vf-surface-2); }
	.ws-row.active { background: var(--vf-surface-3); }
	.ws-row-name { font-weight: var(--vf-fw-medium); font-size: var(--vf-fs-default); }
	.ws-row-id {
		font-size: var(--vf-fs-cap-sm); color: var(--vf-text-secondary);
		font-family: var(--vf-font-mono);
	}
</style>
