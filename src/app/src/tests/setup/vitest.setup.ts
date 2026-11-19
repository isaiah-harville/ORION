import { vi } from 'vitest';

// SvelteKit serves $app/env/public through a global the server injects into
// the page; jsdom has no such global, so the import resolves to undefined here.
vi.mock('$app/env/public', () => ({ PUBLIC_ORION_API_URL: '/api' }));

globalThis.URL.createObjectURL = vi.fn(() => 'blob:mock-url');
