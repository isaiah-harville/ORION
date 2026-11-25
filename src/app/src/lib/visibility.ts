/**
 * Polling that pauses while the tab is hidden.
 *
 * A background tab has nobody looking at it, so refreshing aircraft, quakes or
 * radar there only spends bandwidth -- and keeps the API telling the radar
 * producer someone is watching. Work deferred while hidden runs once, as soon
 * as the tab is shown again.
 */

export function isHidden(): boolean {
	return typeof document !== 'undefined' && document.hidden;
}

/** Run `task` now if the tab is visible, otherwise the moment it becomes so. */
export function runWhenVisible(task: () => void): () => void {
	if (!isHidden()) {
		task();
		return () => undefined;
	}
	const handler = () => {
		if (isHidden()) return;
		document.removeEventListener('visibilitychange', handler);
		task();
	};
	document.addEventListener('visibilitychange', handler);
	return () => document.removeEventListener('visibilitychange', handler);
}

/**
 * Call `task` every `intervalMs` while visible. Returns a stop function.
 *
 * When the tab comes back after missing at least one tick, `task` runs right
 * away instead of waiting out the rest of the interval on stale data.
 */
export function pollWhileVisible(task: () => void, intervalMs: number): () => void {
	let missed = false;
	const interval = window.setInterval(() => {
		if (isHidden()) {
			missed = true;
			return;
		}
		task();
	}, intervalMs);
	const onVisible = () => {
		if (!isHidden() && missed) {
			missed = false;
			task();
		}
	};
	document.addEventListener('visibilitychange', onVisible);
	return () => {
		window.clearInterval(interval);
		document.removeEventListener('visibilitychange', onVisible);
	};
}
