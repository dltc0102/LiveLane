import osmnx as ox

def main():
    custom_filter = (
        '["highway"]'
        '["area"!~"yes"]'
        '["route"!~"ferry"]'
    )
    # ox.settings.overpass_url = "https://overpass.private.coffee/api/interpreter"
    ox.settings.timeout = 300
    data = ox.graph.graph_from_place("Hong Kong", network_type="all", custom_filter=custom_filter)
    data = ox.add_edge_speeds(data)
    data = ox.add_edge_travel_times(data)
    ox.save_graphml(data, "./data/hongkong_full.graphml")

if __name__ == "__main__":
    main()
