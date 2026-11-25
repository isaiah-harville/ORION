<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { buildApiUrl } from '#lib/api.js';
	import { page } from '$app/state';
	import * as maplibregl from 'maplibre-gl';
	import type { StyleSpecification } from 'maplibre-gl';
	import { Button, toast } from '@sivir-ui/svelte';
	import Minus from '@lucide/svelte/icons/minus';
	import Plus from '@lucide/svelte/icons/plus';
	import 'maplibre-gl/dist/maplibre-gl.css';
	// maplibre 6 builds its worker URL at runtime, which the bundler cannot see,
	// so the worker is never emitted and no tiles render. Have Vite bundle it, with
	// the shared chunk it imports, and hand MapLibre the emitted URL instead.
	import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';

	maplibregl.setWorkerUrl(maplibreWorkerUrl);

	import { current_lat_long } from '#lib/stores/current-location.js';
	import {
		loadRadarData,
		RadarUnavailableError,
		isRadarLayerId,
		setRadarFramePosition
	} from '#lib/map-updater.js';
	import { radar_state } from '#lib/runes/current-radar.svelte.js';
	import ADSBLayer from './ADSBLayer.svelte';
	import QuakeLayer from './QuakeLayer.svelte';
	import CommandBar from './CommandBar.svelte';
	import Scrubber from './Scrubber.svelte';
	import StationInspector from './StationInspector.svelte';
	import StatusCluster from './StatusCluster.svelte';
	import { map_state } from '#lib/runes/map-state.svelte.js';
	import { cursor_data } from '#lib/runes/cursor.svelte.js';
	import { layers_state } from '#lib/runes/toggleable-layers.svelte.js';

	import NeonStyle from '#lib/styles/neon.json';
	import DarkStyle from '#lib/styles/dark.json';

	import { map_style_state } from '#lib/runes/map-style.svelte.js';
	import { pollWhileVisible } from '#lib/visibility.js';
	import RadarLayer, { type RadarStation } from './RadarLayer.svelte';
	import AirportsLayer, { type Airport } from './AirportsLayer.svelte';
	import AirportInspector from './AirportInspector.svelte';

	let map: maplibregl.Map | undefined = $state<maplibregl.Map | undefined>();
	let mapElement: HTMLElement;
	let initialView = { lat: 39.8283, long: -98.5795 };
	let apiOnline = $state(false);
	let selectedStation = $state<RadarStation | null>(null);
	let selectedAirport = $state<Airport | null>(null);
	let mapCenter = $state({ lat: initialView.lat, lng: initialView.long });
	let radarFailureNotified = false;
	let apiFailureNotified = false;
	let apiFailureCount = 0;
	let isRadarView = $derived(page.url.pathname === '/');
	let isFlightView = $derived(page.url.pathname === '/adsb');

	/** Wall-clock milliseconds spent crossfading from one frame to the next. */
	const FRAME_DURATION_MS = 2_000;
	/** Extra beat held on the newest frame before the loop restarts. */
	const LOOP_HOLD_FRAMES = 1.5;
	let animationHandle: number | undefined;

	const RETRY_BASE_MS = 2_000;
	const RETRY_CEILING_MS = 30_000;
	let retryDelayMs = RETRY_BASE_MS;
	let retryHandle: ReturnType<typeof setTimeout> | undefined;

	async function updateRadar(
		activeMap: maplibregl.Map,
		timestamp?: number,
		forceManifest = false
	) {
		try {
			await loadRadarData(activeMap, { timestamp, forceManifest });
			radarFailureNotified = false;
			retryDelayMs = RETRY_BASE_MS;
		} catch (error) {
			if (error instanceof DOMException && error.name === 'AbortError') return;
			if (error instanceof RadarUnavailableError) return;
			console.error(error);
			if (apiOnline && !radarFailureNotified) {
				toast.error('Could not load ORION radar data.');
				radarFailureNotified = true;
			}
			// A failure on first paint -- an API still starting, say -- would
			// otherwise leave the map empty until the next scheduled refresh a
			// minute later. Back off from a short delay instead.
			if (retryHandle !== undefined) clearTimeout(retryHandle);
			retryHandle = setTimeout(() => {
				retryHandle = undefined;
				void updateRadar(activeMap, timestamp, true);
			}, retryDelayMs);
			retryDelayMs = Math.min(retryDelayMs * 2, RETRY_CEILING_MS);
		}
	}

	async function checkApiReachability() {
		try {
			const response = await fetch(buildApiUrl('health'));
			if (!response.ok) throw new Error(`Health request failed with ${response.status}`);
			apiOnline = true;
			apiFailureCount = 0;
			apiFailureNotified = false;
		} catch {
			apiOnline = false;
			apiFailureCount += 1;
			if (apiFailureCount >= 3 && !apiFailureNotified) {
				toast.error('Could not reach the ORION API.');
				apiFailureNotified = true;
			}
		}
	}

	function centerSelectedAirport() {
		if (!map || !selectedAirport) return;
		map.flyTo({
			center: [selectedAirport.longitude, selectedAirport.latitude],
			zoom: Math.max(map.getZoom(), 9)
		});
	}

	function centerSelectedStation() {
		if (!map || !selectedStation) return;
		map.flyTo({
			center: [selectedStation.lon, selectedStation.lat],
			zoom: Math.max(map.getZoom(), 10)
		});
	}

	function removeRadarLayers(map: maplibregl.Map): void {
		const style = map.getStyle();
		if (!style?.layers) return;
		style.layers.forEach((layer) => {
			if (isRadarLayerId(layer.id)) {
				if (map.getLayer(layer.id)) map.removeLayer(layer.id);
				if (map.getSource(layer.id)) map.removeSource(layer.id);
			}
		});
	}

	function getMapStyle(style: string) {
		return style === 'neon' ? NeonStyle : DarkStyle;
	}

	function restyleMap() {
		if (!map) return;
		map.setStyle(getMapStyle(map_style_state.data) as StyleSpecification);
		map.once('style.load', () => {
			if (layers_state.data?.radar_layer) {
				void updateRadar(map!, radar_state.radar_state.timestamp);
			}
		});
	}

	function initializeMap() {
		if (map) map.remove();

		const nextMap = new maplibregl.Map({
			container: mapElement,
			style: getMapStyle(map_style_state.data) as StyleSpecification,
			center: [initialView.long, initialView.lat],
			zoom: 8,
			attributionControl: false,
			fadeDuration: 0
		});
		map = nextMap;

		nextMap.setMinZoom(3);
		nextMap.setMaxZoom(24);

		navigator.geolocation.getCurrentPosition(
			({ coords }) => {
				current_lat_long.set({ lat: coords.latitude, long: coords.longitude });
				const el = document.createElement('div');
				el.className = 'orion-user-location';
				el.setAttribute('role', 'img');
				el.setAttribute('aria-label', 'Your current location');
				new maplibregl.Marker({ element: el })
					.setLngLat([coords.longitude, coords.latitude])
					.addTo(nextMap);
			},
			() => undefined
		);

		map_state.data = nextMap;

		nextMap.on('load', () => {
			if (layers_state.data?.radar_layer) {
				void updateRadar(nextMap, radar_state.radar_state.timestamp);
			}
		});

		nextMap.on('click', (event) => {
			selectedStation = null;
			// The airport layer's own click handler sets the selection; only a
			// click on empty map clears it.
			if (
				!nextMap.getLayer('airports-dot') ||
				!nextMap.queryRenderedFeatures(event.point, { layers: ['airports-dot'] }).length
			) {
				selectedAirport = null;
			}
		});
		nextMap.on('move', () => {
			const center = nextMap.getCenter();
			mapCenter = { lat: center.lat, lng: center.lng };
		});

		nextMap.on('mousemove', (e: maplibregl.MapMouseEvent) => {
			cursor_data.clientx = e.originalEvent.clientX;
			cursor_data.clienty = e.originalEvent.clientY;
			cursor_data.lat = parseFloat(e.lngLat.lat.toFixed(6));
			cursor_data.lng = parseFloat(e.lngLat.lng.toFixed(6));
		});
	}

	onMount(() => {
		if ($current_lat_long.lat && $current_lat_long.long) {
			initialView = $current_lat_long;
		}

		initializeMap();
		void checkApiReachability();
		// Thirty seconds is plenty for an online badge, and a hidden tab skips
		// these entirely; both catch up the moment it is shown again.
		const stopApiPolling = pollWhileVisible(checkApiReachability, 30_000);
		const stopRadarPolling = pollWhileVisible(() => {
			if (map && layers_state.data.radar_layer) {
				void updateRadar(map, radar_state.radar_state.timestamp, true);
			}
		}, 60_000);

		const dismissOnEscape = (event: KeyboardEvent) => {
			if (event.key === 'Escape') {
				selectedStation = null;
				selectedAirport = null;
			}
		};
		window.addEventListener('keydown', dismissOnEscape);

		// Advance playback on the display's own cadence so each step is a
		// fractional crossfade rather than a jump between frames.
		let previousTick: number | undefined;
		let drawnPosition: number | undefined;
		const tick = (now: number) => {
			animationHandle = requestAnimationFrame(tick);
			const elapsed = previousTick === undefined ? 0 : now - previousTick;
			previousTick = now;

			const state = radar_state.radar_state;
			const frameCount = state.valid_timestamps.length;
			if (!map || !layers_state.data?.radar_layer || frameCount < 2) return;

			if (state.playing) {
				const advanced = state.position + elapsed / FRAME_DURATION_MS;
				// Hold on the newest frame briefly before looping back.
				state.position = advanced > frameCount - 1 + LOOP_HOLD_FRAMES ? 0 : advanced;

				// Only publish the timestamp when the displayed frame actually
				// changes; writing it every tick would invalidate the scrubber's
				// derived clock sixty times a second.
				const index = Math.round(Math.min(state.position, frameCount - 1));
				const time = state.valid_timestamps[index]?.time;
				if (time !== state.timestamp) state.timestamp = time;
			}
			// Paused playback leaves the position alone; skip re-applying it.
			if (state.position === drawnPosition) return;
			drawnPosition = state.position;
			setRadarFramePosition(map, state.position);
		};
		animationHandle = requestAnimationFrame(tick);

		return () => {
			stopRadarPolling();
			stopApiPolling();
			window.removeEventListener('keydown', dismissOnEscape);
			if (animationHandle !== undefined) cancelAnimationFrame(animationHandle);
		};
	});

	let prevRadarLayer = layers_state.data?.radar_layer;
	let previousMapStyle = map_style_state.data;

	$effect(() => {
		const activeMap = map;
		if (!activeMap) return;

		if (map_style_state.data !== previousMapStyle) {
			previousMapStyle = map_style_state.data;
			restyleMap();
		}

		// Frame changes are handled by the animation loop against preloaded
		// layers, so scrubbing no longer reloads anything.

		if (layers_state.data.radar_layer !== prevRadarLayer) {
			if (layers_state.data.radar_layer === true) {
				removeRadarLayers(activeMap);
				void updateRadar(activeMap, radar_state.radar_state.timestamp, true);
			} else {
				removeRadarLayers(activeMap);
			}
		}

		prevRadarLayer = layers_state.data.radar_layer;

		if (!layers_state.data.radar_stations_layer) selectedStation = null;
		if (!layers_state.data.airports_layer || !isFlightView) selectedAirport = null;
	});

	onDestroy(() => {
		if (animationHandle !== undefined) cancelAnimationFrame(animationHandle);
		if (retryHandle !== undefined) clearTimeout(retryHandle);
		if (map) {
			map.remove();
			map_state.data = undefined;
		}
	});
</script>

<div class="absolute inset-0 h-full w-full" bind:this={mapElement}></div>

{#if map}
	{#key map}
		<CommandBar {map} radarView={isRadarView} />
		<StatusCluster online={apiOnline} />

		<div
			class="absolute top-[76px] z-40 hidden flex-col gap-2 lg:flex"
			class:right-5={!selectedStation && !selectedAirport}
			class:right-[336px]={selectedStation !== null || selectedAirport !== null}
		>
			<Button
				variant="outline"
				size="icon"
				class="orion-surface size-9"
				onclick={() => map?.zoomIn()}
				aria-label="Zoom in"
			>
				<Plus size={16} />
			</Button>
			<Button
				variant="outline"
				size="icon"
				class="orion-surface size-9"
				onclick={() => map?.zoomOut()}
				aria-label="Zoom out"
			>
				<Minus size={16} />
			</Button>
		</div>

		{#if isRadarView}
			<Scrubber center={mapCenter} />
			{#if layers_state.data.radar_layer}
				<div
					class="orion-surface absolute right-5 bottom-6 z-40 hidden flex-col gap-1.5 rounded-[var(--radius-lg)] px-3 py-2.5 lg:flex"
				>
					<span class="text-foreground-muted text-xs font-medium">Reflectivity dBZ</span>
					<div class="flex items-center gap-2">
						<span class="orion-reflectivity-scale h-1.5 w-28 rounded-full"></span>
						<span class="text-foreground-muted font-mono text-[11px]">20 · 40 · 70</span
						>
					</div>
				</div>
			{/if}
		{/if}

		{#if layers_state.data?.adsb_layer}
			<ADSBLayer {map} />
		{/if}
		{#if isFlightView && layers_state.data.airports_layer}
			<AirportsLayer {map} onselect={(airport) => (selectedAirport = airport)} />
		{/if}
		{#if selectedAirport}
			<AirportInspector
				airport={selectedAirport}
				onclose={() => (selectedAirport = null)}
				oncenter={centerSelectedAirport}
			/>
		{/if}
		{#if layers_state.data?.quake_layer}
			<QuakeLayer {map} />
		{/if}
		{#if layers_state.data?.radar_stations_layer}
			<RadarLayer {map} onselect={(station) => (selectedStation = station)} />
		{/if}
		{#if selectedStation}
			<StationInspector
				station={selectedStation}
				onclose={() => (selectedStation = null)}
				oncenter={centerSelectedStation}
				onframes={() =>
					document
						.querySelector<HTMLElement>('#radar-scrubber input[type="range"]')
						?.focus()}
			/>
		{/if}
	{/key}
{/if}
