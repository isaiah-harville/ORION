<script lang="ts">
	import { onMount } from 'svelte';
	import { Button, Card } from '@sivir-ui/svelte';
	import X from '@lucide/svelte/icons/x';
	import { aircraftWithin } from '#lib/adsb-updater.js';
	import type { Airport } from './AirportsLayer.svelte';

	let {
		airport,
		onclose,
		oncenter
	}: { airport: Airport; onclose: () => void; oncenter: () => void } = $props();

	/** Radius for "nearby traffic": roughly the terminal area around a field. */
	const NEARBY_NM = 30;

	let traffic = $state({ airborne: 0, ground: 0 });

	// Aircraft move and the feed refreshes, so recount while the panel is open.
	onMount(() => {
		const count = () =>
			(traffic = aircraftWithin(airport.longitude, airport.latitude, NEARBY_NM));
		count();
		const handle = setInterval(count, 2_000);
		return () => clearInterval(handle);
	});

	let place = $derived([airport.municipality, airport.country].filter((part) => part).join(', '));
</script>

<Card.Root
	variant="panel"
	class="absolute top-[104px] right-3 left-3 z-40 lg:top-[76px] lg:right-5 lg:bottom-auto lg:left-auto lg:w-[300px] [&_[data-ui=card-surface]]:gap-3.5 [&_[data-ui=card-surface]]:p-4"
>
	<div class="flex items-start justify-between gap-2.5">
		<div class="flex min-w-0 flex-col gap-0.5">
			<h2 class="text-base font-semibold tracking-[-0.015em]">
				{airport.iata ?? airport.ident}
				{#if airport.iata}
					<span class="text-foreground-muted font-mono text-xs font-normal"
						>{airport.ident}</span
					>
				{/if}
			</h2>
			<span class="truncate text-[13px]">{airport.name}</span>
			{#if place}
				<span class="text-foreground-muted truncate text-xs">{place}</span>
			{/if}
		</div>
		<Button
			variant="ghost"
			size="icon"
			class="-mt-2 -mr-2 size-9"
			onclick={onclose}
			aria-label="Close airport details"
		>
			<X size={16} />
		</Button>
	</div>

	<dl class="grid grid-cols-2 gap-x-3.5 gap-y-2.5 text-[13px]">
		<div class="flex flex-col gap-0.5">
			<dt class="text-foreground-muted">Elevation</dt>
			<dd class="font-mono tabular-nums">
				{airport.elevation_ft !== null
					? `${Math.round(airport.elevation_ft).toLocaleString()} ft`
					: 'N/A'}
			</dd>
		</div>
		<div class="flex flex-col gap-0.5">
			<dt class="text-foreground-muted">Position</dt>
			<dd class="font-mono tabular-nums">
				{airport.latitude.toFixed(3)}, {airport.longitude.toFixed(3)}
			</dd>
		</div>
		<div class="flex flex-col gap-0.5">
			<dt class="text-foreground-muted">Airborne ≤ {NEARBY_NM} nm</dt>
			<dd class="font-mono tabular-nums">{traffic.airborne}</dd>
		</div>
		<div class="flex flex-col gap-0.5">
			<dt class="text-foreground-muted">On the ground</dt>
			<dd class="font-mono tabular-nums">{traffic.ground}</dd>
		</div>
	</dl>

	<Button onclick={oncenter}>Center map</Button>
</Card.Root>
