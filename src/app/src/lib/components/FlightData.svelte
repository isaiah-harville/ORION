<script lang="ts">
	import { onDestroy } from 'svelte';
	import { Alert, Button, Card } from '@sivir-ui/svelte';
	import { flightStore, type Flight } from '#lib/stores/flight-store.js';
	import { clearSelection, markerColor, label } from '#lib/adsb-updater.js';
	import X from '@lucide/svelte/icons/x';

	let selectedFlight: Flight | null = $state(null);
	let observedAt = $state(0);
	let positionAgeMs = $state(0);
	// Ticks once a second purely so the age below counts up; a value read once
	// at click time is stale before it has finished rendering.
	let clock = $state(0);

	const unsubscribe = flightStore.subscribe((store) => {
		selectedFlight = store.selectedFlight;
		observedAt = store.observedAt;
		positionAgeMs = store.positionAgeMs;
	});

	// Only tick while a flight is actually on screen.
	$effect(() => {
		if (!selectedFlight) return;
		clock = performance.now();
		const handle = setInterval(() => (clock = performance.now()), 1_000);
		return () => clearInterval(handle);
	});

	onDestroy(unsubscribe);

	/** Seconds since the aircraft was actually last heard from, live. */
	let positionAgeSeconds = $derived(
		Math.max(0, Math.round((positionAgeMs + Math.max(0, clock - observedAt)) / 1000))
	);

	function closePanel() {
		clearSelection();
		flightStore.update((d) => ({ ...d, selectedFlight: null }));
	}

	function altitude(flight: Flight): string {
		if (flight.on_ground) return 'On ground';
		return flight.altitude_ft !== null
			? `${Math.round(flight.altitude_ft).toLocaleString()} ft`
			: 'N/A';
	}

	function verticalRate(flight: Flight): string {
		if (flight.vertical_rate_fpm === null) return 'Level';
		const rate = Math.round(flight.vertical_rate_fpm);
		if (Math.abs(rate) < 100) return 'Level';
		return `${rate > 0 ? 'Climbing' : 'Descending'} ${Math.abs(rate).toLocaleString()} fpm`;
	}
</script>

{#if selectedFlight}
	<Card.Root
		variant="panel"
		class="mt-16 w-full lg:w-[27.5rem] [&_[data-ui=card-surface]]:gap-3.5 [&_[data-ui=card-surface]]:p-4"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="flex min-w-0 items-center gap-3">
				<span
					class="size-3 shrink-0 rounded-full"
					style="background-color: {markerColor(selectedFlight)}"
				></span>
				<div class="min-w-0">
					<h2 class="truncate font-mono text-base font-semibold">
						{label(selectedFlight)}
					</h2>
					<div class="text-foreground-muted text-xs">
						{selectedFlight.aircraft_type ?? 'Unknown type'}
						{#if selectedFlight.registration}
							· {selectedFlight.registration}
						{/if}
					</div>
				</div>
			</div>
			<Button
				variant="ghost"
				size="icon"
				class="-mt-2 -mr-2 size-9"
				onclick={closePanel}
				aria-label="Close flight details"
			>
				<X size={16} />
			</Button>
		</div>

		<dl class="grid grid-cols-2 gap-x-3.5 gap-y-2.5 text-[13px]">
			{#each [['Altitude', altitude(selectedFlight)], ['Ground speed', selectedFlight.ground_speed_kt !== null ? `${Math.round(selectedFlight.ground_speed_kt)} kt` : 'N/A'], ['Track', selectedFlight.track_deg !== null ? `${Math.round(selectedFlight.track_deg)}°` : 'N/A'], ['Squawk', selectedFlight.squawk ?? 'N/A'], ['Latitude', selectedFlight.latitude.toFixed(4)], ['Longitude', selectedFlight.longitude.toFixed(4)]] as [term, value] (term)}
				<div class="flex flex-col gap-0.5">
					<dt class="text-foreground-muted">{term}</dt>
					<dd class="font-mono tabular-nums">{value}</dd>
				</div>
			{/each}
			<div class="col-span-2 flex flex-col gap-0.5">
				<dt class="text-foreground-muted">Vertical</dt>
				<dd class="font-mono tabular-nums">{verticalRate(selectedFlight)}</dd>
			</div>
		</dl>

		{#if selectedFlight.emergency}
			<Alert.Root variant="error">
				<Alert.Description>Emergency squawked: {selectedFlight.emergency}</Alert.Description
				>
			</Alert.Root>
		{/if}

		<div class="text-foreground-muted font-mono text-[11px]">
			ICAO {selectedFlight.id.toUpperCase()} · position {positionAgeSeconds}s old
		</div>
	</Card.Root>
{/if}
