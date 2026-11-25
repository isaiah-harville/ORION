import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	aircraftFeatures,
	aircraftWithin,
	AIRCRAFT_LAYER,
	animationStepMs,
	HEATMAP_LAYER,
	HEATMAP_UNTIL_ZOOM,
	AIRCRAFT_SOURCE,
	deadReckon,
	label,
	LEADER_SOURCE,
	loadFlightData,
	markerColor,
	resetAdsbUpdater,
	shortestTurn,
	viewportQuery
} from '#lib/adsb-updater.js';
import { flightStore, type Flight } from '#lib/stores/flight-store.js';
import type { Mock } from 'vitest';
import type * as maplibregl from 'maplibre-gl';

vi.mock('#lib/stores/flight-store.js', () => ({
	flightStore: { update: vi.fn() },
	adsbCoverage: { set: vi.fn() }
}));

const flight = (id: string, overrides: Partial<Flight> = {}): Flight => ({
	id,
	callsign: 'JIA5024',
	registration: 'N586NN',
	aircraft_type: 'CRJ9',
	latitude: 36.674,
	longitude: -88.362,
	altitude_ft: 32000,
	on_ground: false,
	ground_speed_kt: 478,
	track_deg: 243,
	vertical_rate_fpm: 64,
	squawk: '1677',
	emergency: null,
	seen_pos_s: 0,
	distance_nm: 89.9,
	...overrides
});

type Handler = (event: unknown) => void;

/** Enough of a MapLibre map to record what the updater draws. */
function fakeMap() {
	const sources: Record<string, { setData: Mock; data?: unknown }> = {};
	const layers = new Set<string>();
	const handlers: Record<string, Handler> = {};
	const map = {
		getBounds: () => ({
			getWest: () => -90.123,
			getSouth: () => 34.5,
			getEast: () => -84.0,
			getNorth: () => 38.25
		}),
		isStyleLoaded: () => true,
		getZoom: () => 8,
		hasImage: () => true,
		addImage: vi.fn(),
		getSource: (id: string) => sources[id],
		addSource: (id: string) => {
			const source = { setData: vi.fn((data: unknown) => (source.data = data)) } as {
				setData: Mock;
				data?: unknown;
			};
			sources[id] = source;
		},
		getLayer: (id: string) => (layers.has(id) ? { id } : undefined),
		addLayer: (layer: { id: string }) => layers.add(layer.id),
		removeLayer: (id: string) => layers.delete(id),
		removeSource: (id: string) => delete sources[id],
		on: (type: string, layer: string, handler: Handler) =>
			(handlers[`${type}:${layer}`] = handler),
		off: vi.fn(),
		getCanvas: () => ({ style: {} })
	};
	return { map: map as unknown as maplibregl.Map, sources, layers, handlers };
}

function respondWith(aircraft: unknown[], extra: Record<string, unknown> = {}): Mock {
	return vi.fn().mockResolvedValue({
		ok: true,
		json: () => Promise.resolve({ aircraft, ...extra })
	});
}

function positionOf(id: string, now: number): [number, number] {
	const feature = aircraftFeatures(now).aircraft.features.find((f) => f.id === id);
	return feature!.geometry.coordinates as [number, number];
}

describe('adsb layer', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		vi.clearAllMocks();
		resetAdsbUpdater();
	});
	afterEach(() => {
		resetAdsbUpdater();
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	it('queries the whole visible area, not a circle round its centre', async () => {
		const { map } = fakeMap();
		const fetchFn = respondWith([]);
		await loadFlightData(map, fetchFn as unknown as typeof fetch);
		const url = fetchFn.mock.calls[0][0] as string;
		expect(url).toContain('adsb/area?');
		expect(url).toContain('west=-90.12');
		expect(url).toContain('south=34.50');
		expect(url).toContain('east=-84.00');
		expect(url).toContain('north=38.25');
		expect(viewportQuery(map)).toBe(url.slice(url.indexOf('adsb/')));
	});

	it('draws aircraft on a map layer at [lon, lat]', async () => {
		const { map, sources, layers } = fakeMap();
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		expect(layers.has(AIRCRAFT_LAYER)).toBe(true);
		const drawn = sources[AIRCRAFT_SOURCE].data as GeoJSON.FeatureCollection<GeoJSON.Point>;
		expect(drawn.features).toHaveLength(1);
		const [lng, lat] = drawn.features[0].geometry.coordinates;
		// The feed reports lat and lon separately; MapLibre wants [lon, lat].
		expect(lng).toBeCloseTo(-88.362, 2);
		expect(lat).toBeCloseTo(36.674, 2);
		expect(drawn.features[0].properties?.track).toBe(243);
	});

	it('draws a velocity leader a minute ahead along the track', async () => {
		const { map, sources } = fakeMap();
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		const leaders = sources[LEADER_SOURCE]
			.data as GeoJSON.FeatureCollection<GeoJSON.LineString>;
		expect(leaders.features).toHaveLength(1);
		const [[startLon, startLat], [endLon, endLat]] = leaders.features[0].geometry.coordinates;
		// Track 243 is south-west; 478 kt for a minute is about 8 nm.
		expect(endLon).toBeLessThan(startLon);
		expect(endLat).toBeLessThan(startLat);
		const nm = Math.hypot(
			(endLat - startLat) * 60,
			(endLon - startLon) * 60 * Math.cos((startLat * Math.PI) / 180)
		);
		expect(nm).toBeCloseTo(478 / 60, 1);
	});

	it('gives parked aircraft no leader and leaves them where they are', async () => {
		const { map } = fakeMap();
		await loadFlightData(
			map,
			respondWith([
				flight('a', { on_ground: true, ground_speed_kt: 0, altitude_ft: null })
			]) as unknown as typeof fetch
		);
		const now = performance.now();
		expect(aircraftFeatures(now).leaders.features).toHaveLength(0);
		expect(positionOf('a', now + 30_000)).toEqual([-88.362, 36.674]);
	});

	it('draws an aircraft where it is now, not where the report found it', async () => {
		// A report is already stale on arrival; that age is reckoned through so a
		// plane is not drawn permanently trailing itself.
		const { map } = fakeMap();
		await loadFlightData(
			map,
			respondWith([flight('a', { seen_pos_s: 6 })], {
				feed_age_seconds: 14
			}) as unknown as typeof fetch
		);
		const [lng, lat] = positionOf('a', performance.now());
		expect(lng).toBeLessThan(-88.362);
		expect(lat).toBeLessThan(36.674);
	});

	it('keeps flying between reports instead of standing still', async () => {
		const { map } = fakeMap();
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		const start = performance.now();
		const first = positionOf('a', start + 4_000);
		const second = positionOf('a', start + 8_000);
		// Track 243 is south-west, so it must be going that way and no other.
		expect(second[0]).toBeLessThan(first[0]);
		expect(second[1]).toBeLessThan(first[1]);
	});

	it('eases a disagreeing report in rather than snapping to it', async () => {
		const { map } = fakeMap();
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		const before = positionOf('a', performance.now());
		await loadFlightData(
			map,
			respondWith([
				flight('a', { latitude: 37.0, longitude: -88.0 })
			]) as unknown as typeof fetch
		);
		const now = performance.now();
		// Right after the report it is still drawn where it was...
		expect(positionOf('a', now)[1]).toBeCloseTo(before[1], 3);
		// ...and once the correction has blended out it is at the new report.
		expect(positionOf('a', now + 2_000)[1]).toBeGreaterThan(36.99);
	});

	it('removes aircraft the feed no longer reports', async () => {
		const { map } = fakeMap();
		await loadFlightData(
			map,
			respondWith([flight('a'), flight('b')]) as unknown as typeof fetch
		);
		await loadFlightData(map, respondWith([flight('b')]) as unknown as typeof fetch);
		const ids = aircraftFeatures().aircraft.features.map((f) => f.id);
		expect(ids).toEqual(['b']);
	});

	it('selects the aircraft that was clicked', async () => {
		const { map, handlers } = fakeMap();
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		handlers[`click:${AIRCRAFT_LAYER}`]({ features: [{ properties: { id: 'a' } }] });
		expect(flightStore.update).toHaveBeenCalled();
		expect(aircraftFeatures().aircraft.features[0].properties?.selected).toBe(true);
	});

	it('skips aircraft with unusable coordinates', async () => {
		const { map } = fakeMap();
		await loadFlightData(
			map,
			respondWith([flight('a', { latitude: Number.NaN })]) as unknown as typeof fetch
		);
		expect(aircraftFeatures().aircraft.features).toHaveLength(0);
	});

	it('waits for the base style before adding layers', async () => {
		const { map, layers } = fakeMap();
		(map as unknown as { isStyleLoaded: () => boolean }).isStyleLoaded = () => false;
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		expect(layers.size).toBe(0);
	});

	it('survives a failing request without throwing', async () => {
		const { map } = fakeMap();
		const failing = vi.fn().mockResolvedValue({ ok: false, status: 503 });
		await expect(
			loadFlightData(map, failing as unknown as typeof fetch)
		).resolves.toBeUndefined();
	});

	it('shows density instead of glyphs when zoomed far out', async () => {
		const { map, layers } = fakeMap();
		await loadFlightData(map, respondWith([flight('a')]) as unknown as typeof fetch);
		expect(layers.has(HEATMAP_LAYER)).toBe(true);
	});

	it('redraws every frame up close and backs off when far out or crowded', () => {
		expect(animationStepMs(10, 300)).toBe(0);
		expect(animationStepMs(10, 5_000)).toBeGreaterThan(0);
		expect(animationStepMs(HEATMAP_UNTIL_ZOOM - 1, 300)).toBeGreaterThanOrEqual(1_000);
	});

	it('polls sooner while the server is still filling a wide view', async () => {
		const { map } = fakeMap();
		const fetchFn = respondWith([], { pending: 12 });
		await loadFlightData(map, fetchFn as unknown as typeof fetch);
		await vi.advanceTimersByTimeAsync(5_000);
		expect(fetchFn).toHaveBeenCalledTimes(2);
	});

	it('counts nearby traffic for an airport, split by ground and air', async () => {
		const { map } = fakeMap();
		await loadFlightData(
			map,
			respondWith([
				flight('near', { latitude: 36.2, longitude: -86.7 }),
				flight('taxiing', {
					latitude: 36.12,
					longitude: -86.68,
					on_ground: true,
					ground_speed_kt: 0
				}),
				flight('far', { latitude: 40.0, longitude: -80.0 })
			]) as unknown as typeof fetch
		);
		expect(aircraftWithin(-86.678, 36.124, 30)).toEqual({ airborne: 1, ground: 1 });
	});

	it('stops extrapolating when the feed goes quiet', () => {
		// A minute of coasting is a plausible position; ten minutes is fiction.
		const oneMinute = deadReckon([-88.0, 36.0], 480, 90, 60_000);
		const tenMinutes = deadReckon([-88.0, 36.0], 480, 90, 600_000);
		expect(tenMinutes).toEqual(oneMinute);
	});

	it('shortens a degree of longitude with latitude', () => {
		const [equator] = deadReckon([0, 0], 480, 90, 60_000);
		const [arctic] = deadReckon([0, 70], 480, 90, 60_000);
		expect(arctic).toBeGreaterThan(equator);
	});

	it('takes the short way round when a heading crosses north', () => {
		expect(shortestTurn(350, 10)).toBe(20);
		expect(shortestTurn(10, 350)).toBe(-20);
	});

	it('colours by altitude band and flags emergencies', () => {
		expect(markerColor(flight('a', { altitude_ft: 35_000 }))).not.toBe(
			markerColor(flight('a', { altitude_ft: 3_000 }))
		);
		expect(markerColor(flight('a', { on_ground: true, altitude_ft: null }))).not.toBe(
			markerColor(flight('a'))
		);
		expect(markerColor(flight('a', { emergency: 'lifeguard' }))).toBe('#ef4444');
	});

	it('falls back through callsign, registration, then ICAO address', () => {
		expect(label(flight('a'))).toBe('JIA5024');
		expect(label(flight('a', { callsign: null }))).toBe('N586NN');
		expect(label(flight('a78d6b', { callsign: null, registration: null }))).toBe('A78D6B');
	});
});
