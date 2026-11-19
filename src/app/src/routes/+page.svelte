<script lang="ts">
	import { onMount } from 'svelte';
	import { layers_state } from '#lib/runes/toggleable-layers.svelte.js';

	onMount(() => {
		layers_state.data.adsb_layer = false;
		layers_state.data.quake_layer = false;
	});
</script>
