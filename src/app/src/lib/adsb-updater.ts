import type * as maplibregl from 'maplibre-gl';
import type { Feature, FeatureCollection, LineString, Point } from 'geojson';
import { buildApiUrl } from '#lib/api.js';
import { adsbCoverage, flightStore, type Flight } from '#lib/stores/flight-store.js';
import { runWhenVisible } from '#lib/visibility.js';

/**
 * Aircraft move fast enough that a minute-old position is visibly wrong, and
 * the server caches for fifteen seconds anyway, so polling faster than this
 * only costs bandwidth.
 */
export const REFRESH_MS = 15_000;

/**
 * How long a correction takes to blend in when a new report disagrees with
 * where the aircraft was being drawn. Short, because the disagreement is
 * normally small; long enough that the correction is a nudge, not a jump.
 */
const CORRECTION_MS = 2_000;

/**
 * How far ahead of the last report the display is willing to extrapolate. If
 * the feed stops answering, the aircraft coasts for a minute and then holds
 * rather than flying off across the map on stale velocity.
 */
const MAX_EXTRAPOLATION_MS = 60_000;

/**
 * How far ahead each aircraft's velocity leader reaches. At regional zoom a jet
 * covers well under a pixel a second, so position alone reads as stationary;
 * the leader shows where it will be in a minute, which makes speed and heading
 * visible at any zoom.
 */
export const LEADER_MS = 60_000;
/** Below this an aircraft is taxiing or parked; a leader would be noise. */
const LEADER_MIN_KT = 30;

/**
 * Minimum time between redraws, by how much there is to draw and how far out
 * the map is. Each redraw re-sends every aircraft to the map's worker. Zoomed
 * in, that is a few hundred aircraft moving several pixels a second, so every
 * display frame is worth drawing. Zoomed out to a whole region or country it
 * can be thousands moving well under a pixel a second, so a few redraws a
 * second look identical and cost a fraction as much.
 */
export function animationStepMs(zoom: number, count: number): number {
	if (zoom < HEATMAP_UNTIL_ZOOM) return 1_000;
	if (count > 3_000 || zoom < 7) return 250;
	if (count > 1_500) return 50;
	return 0;
}

/**
 * Below this zoom, aircraft are shown as a traffic-density heatmap rather than
 * individual glyphs: at continental scale thousands of overlapping planes are
 * a solid smear, and density is what can actually be read there. The heatmap
 * fades out and the glyphs fade in across the next zoom level.
 */
export const HEATMAP_UNTIL_ZOOM = 6;

/** Poll sooner while the server is still filling a wide view in. */
const PENDING_REFRESH_MS = 4_000;

const MINUTES_PER_DEGREE = 60;

export const AIRCRAFT_SOURCE = 'adsb-aircraft';
export const LEADER_SOURCE = 'adsb-leaders';
export const AIRCRAFT_LAYER = 'adsb-aircraft';
export const LEADER_LAYER = 'adsb-leaders';
export const HEATMAP_LAYER = 'adsb-heatmap';
const AIRCRAFT_ICON = 'adsb-plane';

interface Contact {
	flight: Flight;
	/** The last reported position, and when it arrived. */
	reported: [number, number];
	reportedAt: number;
	speedKt: number;
	track: number;
	/** How stale the report already was when it arrived: the feed's own cache
	 *  age plus the seconds since the aircraft was last heard. */
	positionAgeMs: number;
	/** Difference between where the aircraft was drawn and where the newest
	 *  report puts it, blended out over CORRECTION_MS so it reads as a nudge. */
	offset: [number, number];
	trackOffset: number;
	offsetAt: number;
}

let contacts: Record<string, Contact> = {};
let timer: ReturnType<typeof setTimeout> | undefined;
let cancelDeferred: (() => void) | undefined;
let frame: number | undefined;
let attachedMap: maplibregl.Map | undefined;
let selectedId: string | null = null;
let detachHandlers: (() => void) | undefined;
/** Circles of the current view the server has not filled in yet. */
let pending = 0;

function cancelScheduled(): void {
	if (timer !== undefined) clearTimeout(timer);
	timer = undefined;
	cancelDeferred?.();
	cancelDeferred = undefined;
}

export function resetAdsbUpdater(): void {
	cancelScheduled();
	if (frame !== undefined) cancelAnimationFrame(frame);
	frame = undefined;
	detachHandlers?.();
	detachHandlers = undefined;
	if (attachedMap) removeLayers(attachedMap);
	attachedMap = undefined;
	contacts = {};
	selectedId = null;
	pending = 0;
	adsbCoverage.set({ truncated: false, pending: 0, count: 0 });
}

/** Altitude bands, so a glance separates ground traffic from overflights. */
export function markerColor(flight: Flight): string {
	if (flight.emergency) return '#ef4444';
	if (flight.on_ground) return '#94a3b8';
	const altitude = flight.altitude_ft;
	if (altitude === null) return '#38bdf8';
	if (altitude >= 30_000) return '#c084fc';
	if (altitude >= 18_000) return '#60a5fa';
	if (altitude >= 10_000) return '#34d399';
	return '#fbbf24';
}

export function label(flight: Flight): string {
	return flight.callsign ?? flight.registration ?? flight.id.toUpperCase();
}

/**
 * The signed turn from one heading to another, taking the short way round.
 *
 * Straight arithmetic between 350 and 010 turns the glyph 340 degrees backwards
 * through south, which on screen is an aircraft pirouetting once a refresh.
 */
export function shortestTurn(from: number, to: number): number {
	return ((((to - from) % 360) + 540) % 360) - 180;
}

/**
 * Where an aircraft has flown to since its last report, from its own reported
 * speed and track.
 *
 * Without this the display is a slideshow: reports arrive every fifteen seconds
 * and the server serves a cached copy for fifteen more, so an aircraft drawn
 * straight off report positions sits perfectly still most of the time and then
 * jumps. Dead reckoning between reports is what makes the traffic move.
 */
export function deadReckon(
	position: [number, number],
	speedKt: number,
	track: number,
	elapsedMs: number
): [number, number] {
	const capped = Math.min(Math.max(elapsedMs, 0), MAX_EXTRAPOLATION_MS);
	const nm = speedKt * (capped / 3_600_000);
	if (nm === 0) return [position[0], position[1]];
	const radians = (track * Math.PI) / 180;
	const lat = position[1] + (nm * Math.cos(radians)) / MINUTES_PER_DEGREE;
	// A degree of longitude shortens with latitude; ignoring that skews every
	// east-west track, badly so at Canadian and Alaskan latitudes.
	const shrink = Math.max(Math.cos((lat * Math.PI) / 180), 0.01);
	const lon = position[0] + (nm * Math.sin(radians)) / (MINUTES_PER_DEGREE * shrink);
	return [lon, lat];
}

/** Ease-out, so a correction arrives quickly and settles rather than snapping. */
function ease(t: number): number {
	return 1 - (1 - t) * (1 - t);
}

/** How much of an outstanding correction is still applied at this instant. */
function residual(contact: Contact, now: number): number {
	const t = Math.min(1, Math.max(0, (now - contact.offsetAt) / CORRECTION_MS));
	return 1 - ease(t);
}

function displayedPosition(contact: Contact, now: number): [number, number] {
	const [lon, lat] = deadReckon(
		contact.reported,
		contact.speedKt,
		contact.track,
		contact.positionAgeMs + (now - contact.reportedAt)
	);
	const share = residual(contact, now);
	return [lon + contact.offset[0] * share, lat + contact.offset[1] * share];
}

function displayedTrack(contact: Contact, now: number): number {
	return contact.track + contact.trackOffset * residual(contact, now);
}

/**
 * Every aircraft and its velocity leader where they are at `now`.
 *
 * Exported so tests can step time deterministically without a map.
 */
export function aircraftFeatures(
	now: number = performance.now(),
	withLeaders = true
): {
	aircraft: FeatureCollection<Point>;
	leaders: FeatureCollection<LineString>;
} {
	const aircraft: Feature<Point>[] = [];
	const leaders: Feature<LineString>[] = [];
	for (const [id, contact] of Object.entries(contacts)) {
		const position = displayedPosition(contact, now);
		const track = displayedTrack(contact, now);
		const color = markerColor(contact.flight);
		aircraft.push({
			type: 'Feature',
			id,
			geometry: { type: 'Point', coordinates: position },
			properties: { id, color, track, selected: id === selectedId }
		});
		if (withLeaders && contact.speedKt >= LEADER_MIN_KT) {
			leaders.push({
				type: 'Feature',
				geometry: {
					type: 'LineString',
					coordinates: [position, deadReckon(position, contact.speedKt, track, LEADER_MS)]
				},
				properties: { color }
			});
		}
	}
	return {
		aircraft: { type: 'FeatureCollection', features: aircraft },
		leaders: { type: 'FeatureCollection', features: leaders }
	};
}

/** Push the current positions to the map. Exported for tests. */
export function stepAnimation(now: number = performance.now()): void {
	const map = attachedMap;
	if (!map || !ensureLayers(map)) return;
	const { aircraft, leaders } = aircraftFeatures(now, map.getZoom() >= HEATMAP_UNTIL_ZOOM - 0.5);
	(map.getSource(AIRCRAFT_SOURCE) as maplibregl.GeoJSONSource | undefined)?.setData(aircraft);
	(map.getSource(LEADER_SOURCE) as maplibregl.GeoJSONSource | undefined)?.setData(leaders);
}

/**
 * A plane glyph as a signed-distance-field icon, so one image takes any colour
 * through `icon-color` instead of one image per altitude band.
 */
function planeIcon(): ImageData | undefined {
	if (typeof document === 'undefined' || typeof Path2D === 'undefined') return undefined;
	const size = 48;
	const canvas = document.createElement('canvas');
	canvas.width = size;
	canvas.height = size;
	const context = canvas.getContext('2d');
	if (!context) return undefined;
	context.scale(size / 24, size / 24);
	context.fillStyle = '#fff';
	context.fill(
		new Path2D(
			'M12 2l1.6 6.5 7.4 3v2l-7.4-1.6-.9 5.3 2.8 2v1.8L12 20l-3.5 1v-1.8l2.8-2-.9-5.3L3 13.5v-2l7.4-3z'
		)
	);
	return context.getImageData(0, 0, size, size);
}

/**
 * Add the aircraft sources and layers if they are missing. A base-style swap
 * drops every source, so this is checked on each redraw rather than once.
 * Returns false while the style is still loading.
 */
function ensureLayers(map: maplibregl.Map): boolean {
	if (map.getSource(AIRCRAFT_SOURCE) && map.getLayer(AIRCRAFT_LAYER)) return true;
	if (!map.isStyleLoaded()) return false;

	const empty: FeatureCollection = { type: 'FeatureCollection', features: [] };
	if (!map.hasImage(AIRCRAFT_ICON)) {
		const icon = planeIcon();
		if (icon) map.addImage(AIRCRAFT_ICON, icon, { sdf: true, pixelRatio: 2 });
	}
	if (!map.getSource(LEADER_SOURCE))
		map.addSource(LEADER_SOURCE, { type: 'geojson', data: empty });
	if (!map.getSource(AIRCRAFT_SOURCE))
		map.addSource(AIRCRAFT_SOURCE, { type: 'geojson', data: empty });
	const fadeIn = (to: number): maplibregl.ExpressionSpecification => [
		'interpolate',
		['linear'],
		['zoom'],
		HEATMAP_UNTIL_ZOOM - 0.5,
		0,
		HEATMAP_UNTIL_ZOOM + 0.5,
		to
	];
	if (!map.getLayer(HEATMAP_LAYER)) {
		map.addLayer({
			id: HEATMAP_LAYER,
			type: 'heatmap',
			source: AIRCRAFT_SOURCE,
			maxzoom: HEATMAP_UNTIL_ZOOM + 1,
			paint: {
				'heatmap-weight': 1,
				'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 2, 0.25, 6, 0.7],
				'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 2, 4, 6, 11],
				'heatmap-color': [
					'interpolate',
					['linear'],
					['heatmap-density'],
					0,
					'rgba(56, 189, 248, 0)',
					0.15,
					'rgba(56, 189, 248, 0.35)',
					0.4,
					'rgba(96, 165, 250, 0.6)',
					0.7,
					'rgba(192, 132, 252, 0.8)',
					1,
					'rgba(255, 255, 255, 0.95)'
				],
				'heatmap-opacity': [
					'interpolate',
					['linear'],
					['zoom'],
					HEATMAP_UNTIL_ZOOM - 0.5,
					0.9,
					HEATMAP_UNTIL_ZOOM + 0.5,
					0
				]
			}
		});
	}
	if (!map.getLayer(LEADER_LAYER)) {
		map.addLayer({
			id: LEADER_LAYER,
			type: 'line',
			source: LEADER_SOURCE,
			minzoom: HEATMAP_UNTIL_ZOOM - 0.5,
			layout: { 'line-cap': 'round' },
			paint: {
				'line-color': ['get', 'color'],
				'line-width': 1.5,
				'line-opacity': fadeIn(0.65)
			}
		});
	}
	if (!map.getLayer(AIRCRAFT_LAYER)) {
		map.addLayer({
			id: AIRCRAFT_LAYER,
			type: 'symbol',
			source: AIRCRAFT_SOURCE,
			minzoom: HEATMAP_UNTIL_ZOOM - 0.5,
			layout: {
				'icon-image': AIRCRAFT_ICON,
				'icon-size': ['interpolate', ['linear'], ['zoom'], 4, 0.7, 8, 0.85, 12, 1.1],
				'icon-rotate': ['get', 'track'],
				'icon-rotation-alignment': 'map',
				'icon-allow-overlap': true,
				'icon-ignore-placement': true
			},
			paint: {
				'icon-color': ['get', 'color'],
				'icon-opacity': fadeIn(1),
				'icon-halo-color': ['case', ['get', 'selected'], '#ffffff', 'rgba(0, 0, 0, 0.85)'],
				'icon-halo-width': ['case', ['get', 'selected'], 2, 1]
			}
		});
	}
	return true;
}

function removeLayers(map: maplibregl.Map): void {
	try {
		for (const layer of [AIRCRAFT_LAYER, LEADER_LAYER, HEATMAP_LAYER]) {
			if (map.getLayer(layer)) map.removeLayer(layer);
		}
		for (const source of [AIRCRAFT_SOURCE, LEADER_SOURCE]) {
			if (map.getSource(source)) map.removeSource(source);
		}
	} catch {
		// The map may already be torn down with its style.
	}
}

function select(id: string): void {
	const contact = contacts[id];
	if (!contact) return;
	selectedId = id;
	flightStore.update((store) => ({
		...store,
		selectedFlight: contact.flight,
		observedAt: contact.reportedAt,
		positionAgeMs: contact.positionAgeMs
	}));
}

function attach(map: maplibregl.Map): void {
	if (attachedMap === map) return;
	detachHandlers?.();
	attachedMap = map;

	const onClick = (event: maplibregl.MapLayerMouseEvent) => {
		const id = event.features?.[0]?.properties?.id;
		if (typeof id === 'string') select(id);
	};
	const onEnter = () => (map.getCanvas().style.cursor = 'pointer');
	const onLeave = () => (map.getCanvas().style.cursor = '');
	map.on('click', AIRCRAFT_LAYER, onClick);
	map.on('mouseenter', AIRCRAFT_LAYER, onEnter);
	map.on('mouseleave', AIRCRAFT_LAYER, onLeave);
	detachHandlers = () => {
		map.off('click', AIRCRAFT_LAYER, onClick);
		map.off('mouseenter', AIRCRAFT_LAYER, onEnter);
		map.off('mouseleave', AIRCRAFT_LAYER, onLeave);
	};
}

function startAnimation(): void {
	if (frame !== undefined) return;
	let last = -Infinity;
	const tick = (now: number) => {
		frame = requestAnimationFrame(tick);
		const map = attachedMap;
		if (!map) return;
		if (now - last < animationStepMs(map.getZoom(), Object.keys(contacts).length)) return;
		last = now;
		stepAnimation(now);
	};
	frame = requestAnimationFrame(tick);
}

/** The visible area, rounded so tiny pans do not defeat HTTP caching. */
export function viewportQuery(map: maplibregl.Map): string {
	const bounds = map.getBounds();
	const round = (value: number) => value.toFixed(2);
	return (
		`adsb/area?west=${round(bounds.getWest())}&south=${round(bounds.getSouth())}` +
		`&east=${round(bounds.getEast())}&north=${round(bounds.getNorth())}`
	);
}

export async function loadFlightData(
	map: maplibregl.Map,
	fetchFn: typeof fetch = fetch
): Promise<void> {
	attach(map);
	try {
		const response = await fetchFn(buildApiUrl(viewportQuery(map)));
		if (!response.ok) throw new Error(`ADS-B request failed with ${response.status}`);
		const payload = (await response.json()) as {
			aircraft?: Flight[];
			feed_age_seconds?: number;
			truncated?: boolean;
			pending?: number;
		};
		pending = payload.pending ?? 0;
		const aircraft = payload.aircraft ?? [];
		const now = performance.now();
		// A response can describe a sky some seconds old before a single
		// aircraft's own report age is counted. Both have to be reckoned
		// through, or every aircraft is drawn permanently behind where it is.
		const feedAgeMs = (payload.feed_age_seconds ?? 0) * 1000;

		const seen = new Set<string>();
		for (const flight of aircraft) {
			if (
				!flight?.id ||
				!Number.isFinite(flight.longitude) ||
				!Number.isFinite(flight.latitude)
			)
				continue;
			seen.add(flight.id);
			const reported: [number, number] = [flight.longitude, flight.latitude];
			const track = flight.track_deg ?? 0;
			const speedKt = flight.on_ground ? 0 : (flight.ground_speed_kt ?? 0);
			const positionAgeMs = feedAgeMs + (flight.seen_pos_s ?? 0) * 1000;
			const existing = contacts[flight.id];

			if (existing) {
				// Carry the current on-screen position into the new report as a
				// correction, so a report that disagrees with the dead-reckoned
				// track eases into place instead of snapping.
				const wasAt = displayedPosition(existing, now);
				const wasFacing = displayedTrack(existing, now);
				existing.flight = flight;
				existing.reported = reported;
				existing.reportedAt = now;
				existing.speedKt = speedKt;
				existing.track = track;
				existing.positionAgeMs = positionAgeMs;
				const nowAt = displayedPosition({ ...existing, offset: [0, 0] }, now);
				existing.offset = [wasAt[0] - nowAt[0], wasAt[1] - nowAt[1]];
				existing.trackOffset = shortestTurn(track, wasFacing);
				existing.offsetAt = now;
			} else {
				contacts[flight.id] = {
					flight,
					reported,
					reportedAt: now,
					speedKt,
					track,
					positionAgeMs,
					offset: [0, 0],
					trackOffset: 0,
					offsetAt: now
				};
			}
		}

		// Aircraft land, leave the viewport, or go out of receiver range, so
		// drop what the feed no longer lists.
		for (const id of Object.keys(contacts)) {
			if (!seen.has(id)) delete contacts[id];
		}
		// Keep the open detail panel on the newest report.
		if (selectedId && contacts[selectedId]) select(selectedId);

		adsbCoverage.set({ truncated: payload.truncated ?? false, pending, count: seen.size });
		stepAnimation(now);
		startAnimation();
	} catch (error) {
		console.error('ADS-B fetch error:', error);
	} finally {
		cancelScheduled();
		// A hidden tab has nobody watching: hold the next poll until it is shown.
		timer = setTimeout(
			() => {
				timer = undefined;
				cancelDeferred = runWhenVisible(() => void loadFlightData(map, fetchFn));
			},
			pending > 0 ? PENDING_REFRESH_MS : REFRESH_MS
		);
	}
}

/** Re-query for a moved viewport, keeping the aircraft already drawn. */
export function refreshForViewport(map: maplibregl.Map, fetchFn: typeof fetch = fetch): void {
	cancelScheduled();
	void loadFlightData(map, fetchFn);
}

/** Forget the selection, e.g. when the detail panel is closed. */
export function clearSelection(): void {
	selectedId = null;
}

/**
 * Aircraft currently drawn within `nm` nautical miles of a point, airborne
 * and on the ground. Used by the airport panel for nearby traffic.
 */
export function aircraftWithin(
	lon: number,
	lat: number,
	nm: number,
	now: number = performance.now()
): { airborne: number; ground: number } {
	let airborne = 0;
	let ground = 0;
	const shrink = Math.cos((lat * Math.PI) / 180);
	for (const contact of Object.values(contacts)) {
		const [x, y] = displayedPosition(contact, now);
		const distance = Math.hypot(
			(y - lat) * MINUTES_PER_DEGREE,
			(x - lon) * MINUTES_PER_DEGREE * shrink
		);
		if (distance > nm) continue;
		if (contact.flight.on_ground) ground += 1;
		else airborne += 1;
	}
	return { airborne, ground };
}
