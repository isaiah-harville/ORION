<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type * as maplibregl from 'maplibre-gl';
	import { loadFlightData, refreshForViewport, resetAdsbUpdater } from '#lib/adsb-updater.js';
	import { adsbCoverage } from '#lib/stores/flight-store.js';

	let { map }: { map: maplibregl.Map } = $props();

	/**
	 * ADS-B is queried for the visible area, so a pan or zoom has to re-query or
	 * the map keeps showing traffic from where the user used to be. Debounced
	 * past the inertia so dragging across a state is one request. Aircraft
	 * already on screen are kept and moved, never rebuilt.
	 */
	let moveTimer: ReturnType<typeof setTimeout> | undefined;

	function onMoveEnd() {
		if (moveTimer !== undefined) clearTimeout(moveTimer);
		moveTimer = setTimeout(() => refreshForViewport(map), 600);
	}

	onMount(() => {
		resetAdsbUpdater();
		void loadFlightData(map);
		map.on('moveend', onMoveEnd);
	});

	onDestroy(() => {
		if (moveTimer !== undefined) clearTimeout(moveTimer);
		map.off('moveend', onMoveEnd);
		resetAdsbUpdater();
	});
</script>

{#if $adsbCoverage.pending > 0 || $adsbCoverage.truncated}
	<div
		class="orion-surface text-foreground-muted pointer-events-none absolute bottom-6 left-1/2 z-30 -translate-x-1/2 rounded-full px-3 py-1.5 text-xs"
		role="status"
	>
		{#if $adsbCoverage.pending > 0}
			Loading wider coverage · {$adsbCoverage.pending} areas to go
		{:else}
			Showing traffic near the centre · zoom in for full coverage
		{/if}
	</div>
{/if}
