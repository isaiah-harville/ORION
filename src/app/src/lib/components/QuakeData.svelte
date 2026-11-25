<script lang="ts">
	import { onDestroy } from 'svelte';
	import { quakeStore, type Quake } from '#lib/stores/quake-store.js';
	import { Alert, Button, Card } from '@sivir-ui/svelte';
	import { markerColor } from '#lib/quake-updater.js';
	import X from '@lucide/svelte/icons/x';

	let selectedQuake: Quake | null = $state(null);

	const unsubscribe = quakeStore.subscribe((store) => {
		selectedQuake = store.selectedQuake;
	});

	onDestroy(unsubscribe);

	function closePanel() {
		quakeStore.update((d) => ({ ...d, selectedQuake: null }));
	}

	function formatTime(milliseconds: number): string {
		return new Date(milliseconds).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
	}
</script>

{#if selectedQuake}
	<Card.Root
		variant="panel"
		class="mt-16 w-full lg:w-[27.5rem] [&_[data-ui=card-surface]]:gap-3.5 [&_[data-ui=card-surface]]:p-4"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="flex min-w-0 items-center gap-3">
				<span
					class="flex size-11 shrink-0 items-center justify-center rounded-full font-mono text-sm font-bold text-black"
					style="background-color: {markerColor(selectedQuake.magnitude)}"
				>
					{selectedQuake.magnitude.toFixed(1)}
				</span>
				<div class="min-w-0">
					<h2 class="truncate text-base font-semibold">
						{selectedQuake.place ?? 'Unknown location'}
					</h2>
					<div class="text-foreground-muted font-mono text-xs">
						{formatTime(selectedQuake.time)}
					</div>
				</div>
			</div>
			<Button
				variant="ghost"
				size="icon"
				class="-mt-2 -mr-2 size-9"
				onclick={closePanel}
				aria-label="Close earthquake details"
			>
				<X size={16} />
			</Button>
		</div>

		<dl class="grid grid-cols-2 gap-x-3.5 gap-y-2.5 text-[13px]">
			{#each [['Latitude', selectedQuake.latitude.toFixed(4)], ['Longitude', selectedQuake.longitude.toFixed(4)], ['Depth', selectedQuake.depth_km !== null ? `${selectedQuake.depth_km.toFixed(1)} km` : 'N/A'], ['Felt reports', String(selectedQuake.felt ?? 0)]] as [term, value] (term)}
				<div class="flex flex-col gap-0.5">
					<dt class="text-foreground-muted">{term}</dt>
					<dd class="font-mono tabular-nums">{value}</dd>
				</div>
			{/each}
		</dl>

		{#if selectedQuake.tsunami}
			<Alert.Root variant="warning">
				<Alert.Description>Tsunami evaluation issued for this event</Alert.Description>
			</Alert.Root>
		{/if}

		{#if selectedQuake.url}
			<!-- An absolute USGS event URL, not an app route, so resolve() does not apply. -->
			<Button
				variant="outline"
				href={selectedQuake.url}
				target="_blank"
				rel="noopener noreferrer"
			>
				View on USGS
			</Button>
		{/if}
	</Card.Root>
{/if}
