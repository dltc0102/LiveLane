import osmnx as ox

custom_filter = (
    '["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"]'
    '["area"!~"yes"]'
    '["motor_vehicle"!~"no"]'
    '["motorcar"!~"no"]'
    '["access"!~"private"]'
)
G = ox.graph.graph_from_place("Hong Kong", network_type="drive", custom_filter=custom_filter)
G = ox.add_edge_speeds(G)
G = ox.add_edge_travel_times(G)
ox.save_graphml(G, filepath="./data/hongkong_drive.graphml")
