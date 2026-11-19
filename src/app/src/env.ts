import { defineEnvVars } from '@sveltejs/kit/env';

export const variables = defineEnvVars({
	PUBLIC_ORION_API_URL: {
		public: true,
		schema: (value) => value || '/api',
		description: 'Base URL for the ORION API. Defaults to a same-origin /api.'
	}
});
