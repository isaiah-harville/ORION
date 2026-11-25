export function create_layers_state() {
	let data = $state<{
		radar_layer: boolean;
		radar_stations_layer: boolean;
		adsb_layer: boolean;
		airports_layer: boolean;
		quake_layer: boolean;
	}>({
		radar_layer: true,
		radar_stations_layer: false,
		adsb_layer: false,
		airports_layer: true,
		quake_layer: false
	});

	return {
		get data() {
			return data;
		},
		set data(newState) {
			data = newState;
		}
	};
}

export const layers_state = create_layers_state();
