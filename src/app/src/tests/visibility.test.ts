import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { pollWhileVisible, runWhenVisible } from '#lib/visibility.js';

function setHidden(hidden: boolean) {
	Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
	document.dispatchEvent(new Event('visibilitychange'));
}

describe('visibility-aware polling', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		setHidden(false);
	});
	afterEach(() => {
		vi.useRealTimers();
		setHidden(false);
	});

	it('runs immediately while visible', () => {
		const task = vi.fn();
		runWhenVisible(task);
		expect(task).toHaveBeenCalledOnce();
	});

	it('defers work until a hidden tab is shown', () => {
		setHidden(true);
		const task = vi.fn();
		runWhenVisible(task);
		expect(task).not.toHaveBeenCalled();
		setHidden(false);
		expect(task).toHaveBeenCalledOnce();
	});

	it('skips ticks while hidden and catches up once on return', () => {
		const task = vi.fn();
		const stop = pollWhileVisible(task, 1_000);
		vi.advanceTimersByTime(1_000);
		expect(task).toHaveBeenCalledTimes(1);

		setHidden(true);
		vi.advanceTimersByTime(5_000);
		expect(task).toHaveBeenCalledTimes(1);

		setHidden(false);
		expect(task).toHaveBeenCalledTimes(2);
		stop();
		vi.advanceTimersByTime(5_000);
		expect(task).toHaveBeenCalledTimes(2);
	});
});
