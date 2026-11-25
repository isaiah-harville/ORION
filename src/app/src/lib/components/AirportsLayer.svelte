<script module lang="ts">
	export type Airport = {
		ident: string;
		iata: string | null;
		name: string;
		latitude: number;
		longitude: number;
		elevation_ft: number | null;
		municipality: string | null;
		country: string;
	};

	/** The list never changes within a session; fetch it once per page load. */
	let airportsRequest: Promise<Airport[]> | undefined;
</script>

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type * as maplibregl from 'maplibre-gl';
	import type { FeatureCollection, Point } from 'geojson';
	import { toast } from '@sivir-ui/svelte';
	import { buildApiUrl } from '#lib/api.js';
	import { AIRCRAFT_LAYER, HEATMAP_LAYER } from '#lib/adsb-updater.js';

	const { map, onselect }: { map: maplibregl.Map; onselect: (airport: Airport) => void } =
		$props();

	const SOURCE = 'airports';
	const DOT_LAYER = 'airports-dot';
	const LABEL_LAYER = 'airports-label';

	let airports: Airport[] = [];
	let byIdent = new Map<string, Airport>();

	function loadAirports(): Promise<Airport[]> {
		airportsRequest ??= fetch(buildApiUrl('airports'))
			.then((response) => {
				if (!response.ok) throw new Error(`Airport request failed with ${response.status}`);
				return response.json() as Promise<{ airports: Airport[] }>;
			})
			.then((payload) => payload.airports)
			.catch((error) => {
				airportsRequest = undefined;
				throw error;
			});
		return airportsRequest;
	}

	function features(): FeatureCollection<Point> {
		return {
			type: 'FeatureCollection',
			features: airports.map((airport) => ({
				type: 'Feature',
				geometry: { type: 'Point', coordinates: [airport.longitude, airport.latitude] },
				properties: { ident: airport.ident, code: airport.iata ?? airport.ident }
			}))
		};
	}

	/**
	 * Add the airport source and layers if missing. A base-style swap drops
	 * them, so this also runs on every style change. Airports sit under the
	 * aircraft so traffic over a field stays clickable.
	 */
	function ensureLayers() {
		if (!airports.length || !map.isStyleLoaded()) return;
		if (!map.getSource(SOURCE)) map.addSource(SOURCE, { type: 'geojson', data: features() });
		const below = map.getLayer(HEATMAP_LAYER) ? HEATMAP_LAYER : undefined;
		if (!map.getLayer(DOT_LAYER)) {
			map.addLayer(
				{
					id: DOT_LAYER,
					type: 'circle',
					source: SOURCE,
					paint: {
						'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 2.5, 10, 6],
						'circle-color': 'rgba(226, 232, 240, 0.12)',
						'circle-stroke-color': '#e2e8f0',
						'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 3, 1, 10, 2],
						'circle-stroke-opacity': 0.85
					}
				},
				below
			);
		}
		if (!map.getLayer(LABEL_LAYER)) {
			map.addLayer(
				{
					id: LABEL_LAYER,
					type: 'symbol',
					source: SOURCE,
					minzoom: 5,
					layout: {
						'text-field': ['get', 'code'],
						'text-font': ['Noto Sans Regular'],
						'text-size': 11,
						'text-offset': [0, 0.9],
						'text-anchor': 'top',
						'text-letter-spacing': 0.05
					},
					paint: {
						'text-color': '#cbd5e1',
						'text-halo-color': 'rgba(0, 0, 0, 0.85)',
						'text-halo-width': 1.2
					}
				},
				below
			);
		}
	}

	function removeLayers() {
		try {
			for (const layer of [LABEL_LAYER, DOT_LAYER])
				if (map.getLayer(layer)) map.removeLayer(layer);
			if (map.getSource(SOURCE)) map.removeSource(SOURCE);
		} catch {
			// The map may already be torn down with its style.
		}
	}

	function onClick(event: maplibregl.MapLayerMouseEvent) {
		// Aircraft are drawn above airports; a click on one is for the aircraft.
		if (
			map.getLayer(AIRCRAFT_LAYER) &&
			map.queryRenderedFeatures(event.point, { layers: [AIRCRAFT_LAYER] }).length
		)
			return;
		const ident = event.features?.[0]?.properties?.ident;
		const airport = typeof ident === 'string' ? byIdent.get(ident) : undefined;
		if (airport) onselect(airport);
	}
	const onEnter = () => (map.getCanvas().style.cursor = 'pointer');
	const onLeave = () => (map.getCanvas().style.cursor = '');

	onMount(async () => {
		map.on('styledata', ensureLayers);
		map.on('click', DOT_LAYER, onClick);
		map.on('mouseenter', DOT_LAYER, onEnter);
		map.on('mouseleave', DOT_LAYER, onLeave);
		try {
			airports = await loadAirports();
			byIdent = new Map(airports.map((airport) => [airport.ident, airport]));
			ensureLayers();
		} catch (error) {
			console.error(error);
			toast.error('Could not load airports.');
		}
	});

	onDestroy(() => {
		map.off('styledata', ensureLayers);
		map.off('click', DOT_LAYER, onClick);
		map.off('mouseenter', DOT_LAYER, onEnter);
		map.off('mouseleave', DOT_LAYER, onLeave);
		removeLayers();
	});
</script>
