import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	cacheDir: '../../.cache/vite',
	plugins: [
		tailwindcss(),
		sveltekit({
			adapter: adapter(),
			preprocess: vitePreprocess()
		})
	],
	// MapLibre's worker is an ES module that imports a shared chunk.
	worker: {
		format: 'es'
	},
	optimizeDeps: {
		exclude: ['maplibre-gl']
	},
	server: {
		allowedHosts: ['orion.harville.dev', 'localhost', '127.0.0.1'],
		host: '0.0.0.0',
		port: 5173,
		watch: {
			usePolling: true
		}
	},
	test: {
		environment: 'jsdom',
		globals: true,
		include: ['src/**/*.{test,spec}.{js,ts}'],
		setupFiles: ['./src/tests/setup/vitest.setup.ts']
	}
});
