<script lang="ts">
	let { title, body, ctaHref, ctaLabel } = $props<{
		title: string;
		body: string;
		ctaHref: string;
		ctaLabel: string;
	}>();
</script>

<section class="empty-state" data-testid="vf-empty-state">
	<h1>{title}</h1>
	<p>{body}</p>
	<a class="vf-btn vf-btn-primary vf-btn-lg" href={ctaHref}>{ctaLabel}</a>
</section>

<style>
	.empty-state {
		text-align: center;
		padding: 96px var(--vf-sp-7);
		max-width: 560px;
		margin: 0 auto;
	}
	.empty-state h1 { margin-bottom: var(--vf-sp-3); }
	.empty-state p {
		color: var(--vf-text-secondary);
		margin-bottom: var(--vf-sp-6);
	}
</style>
