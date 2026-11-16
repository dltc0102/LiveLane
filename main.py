"""
Program Purpose: A* routing engine that finds the fastest path from point A to point B.
"""

import os, json, httpx, asyncio, folium, math, shutil
import osmnx as ox
import networkx as nx
from osmnx import geocoder as oxgeo
from urllib.parse import quote
from haversine import haversine

def clear_dir(dirpath: str) -> None:
    if os.path.exists(dirpath):
        shutil.rmtree(dirpath)
    os.makedirs(dirpath)
    return

def nominatim_addr_to_coords(given_addr: str) -> tuple[float, float]:
    return oxgeo.geocode(given_addr)

async def fetch_url(client: httpx.AsyncClient, url: str) -> tuple[str, int]:
    response = await client.get(url)
    response.raise_for_status()
    return response
 
async def address_lookup(client: httpx.AsyncClient, given_addr: str):
    encoded_address: str = _encode_address(given_addr)
    url = f"https://www.map.gov.hk/gs/api/v1.0.0/locationSearch?q={encoded_address}"
    response = await fetch_url(client, url)
    return response.json()

def dump_json(input_data, output_fpath):
    with open(output_fpath, "w", encoding='utf-8') as f:
        json.dump(input_data, f, ensure_ascii=False, indent=2)

def _encode_address(given_addr: str) -> str:
    return quote(given_addr)

def download_osmnx_data(osm_place="Hong Kong", network_type="drive") -> nx.MultiDiGraph:
    print("Downloading OSM data...")
    hk_driving_data = ox.graph.graph_from_place(osm_place, network_type=network_type)
    hk_driving_data = ox.add_edge_speeds(hk_driving_data)
    hk_driving_data = ox.add_edge_travel_times(hk_driving_data)
    return hk_driving_data

def add_traffic_light_penalties(graph, penalty_seconds=25):
    """time penalty added for each edge that ends with a traffic light"""
    count = 0
    for u, v, k, data in graph.edges(keys=True, data=True):
        if graph.nodes[v].get('highway') == 'traffic_signals':
            data['travel_time'] += penalty_seconds
            count += 1
    print(f"🚦 Added {penalty_seconds}s penalty to {count} edges entering traffic lights.")
    return graph

def hk_heuristic(u, v, graph):
    """Haversine distance / Max Speed (110 km/h) to ensure admissibility"""
    lat_u, lon_u = graph.nodes[u]['y'], graph.nodes[u]['x']
    lat_v, lon_v = graph.nodes[v]['y'], graph.nodes[v]['x']
    dist_km = haversine((lat_u, lon_u), (lat_v, lon_v))
    return (dist_km / 110) * 3600 

def create_node(modal_data, details):
    # osmnx uses lon for x, lat for y
    return ox.distance.nearest_nodes(modal_data, X=details['lon'], Y=details['lat'])

def get_cartesian(details) -> tuple[float, float, float]:
    lat_r, lon_r = math.radians(details['lat']), math.radians(details['lon'])
    x = math.cos(lat_r) * math.cos(lon_r)
    y = math.cos(lat_r) * math.sin(lon_r)
    z = math.sin(lat_r)
    return x, y, z

def to_latlon(x, y, z) -> tuple[float, float]:
    lon = math.atan2(y, x)
    lat = math.atan2(z, math.sqrt(x*x + y*y))
    return math.degrees(lat), math.degrees(lon)

def calculate_center(start, end) -> tuple[float, float]:
    p1 = get_cartesian(start)
    p2 = get_cartesian(end)
    x, y, z = [(a + b) / 2 for a, b in zip(p1, p2)]
    norm = math.sqrt(x*x + y*y + z*z)
    return to_latlon(x/norm, y/norm, z/norm)

def create_navigation(modal_data, start_details, end_details) -> tuple:
    start_node = create_node(modal_data, start_details)
    end_node = create_node(modal_data, end_details)
    center_coords = calculate_center(start_details, end_details)
    return start_node, end_node, center_coords


async def main():
    clear_dir("outputs")
    hk_driving_cache_path = "./data/hongkong_drive.graphml"
    
    if os.path.exists(hk_driving_cache_path):
        print("HK Driving Cache exists, loading...")
        hk_drive_data = ox.load_graphml(hk_driving_cache_path)
    else:
        print("HK Driving Cache doesn't exist, grabbing...")
        hk_drive_data = download_osmnx_data()
        ox.save_graphml(hk_drive_data, filepath=hk_driving_cache_path)

    hk_drive_data = add_traffic_light_penalties(hk_drive_data, penalty_seconds=25)

    home_details = {'lat': 22.3638, 'lon': 114.1791} # World-Wide Gardens
    belchers_details = {'lat': 22.2860, 'lon': 114.1329} # Belchers

    start_node, end_node, center_coords = create_navigation(
        modal_data=hk_drive_data, 
        start_details=home_details, 
        end_details=belchers_details
    )

    print("Calculating A* route...")
    astar_route = nx.astar_path(
        hk_drive_data, 
        start_node, 
        end_node, 
        heuristic=lambda u, v: hk_heuristic(u, v, hk_drive_data), 
        weight="travel_time"
    )
    
    total_time = sum(
        min(hk_drive_data[u][v].values(), key=lambda d: d["travel_time"])["travel_time"]
        for u, v in zip(astar_route[:-1], astar_route[1:])
    )
    total_dist = sum(
        min(hk_drive_data[u][v].values(), key=lambda d: d["length"])["length"]
        for u, v in zip(astar_route[:-1], astar_route[1:])
    )
    print(f"✅ Result: {len(astar_route)} nodes, {total_time/60:.1f} min, {total_dist/1000:.2f} km")

    route_gdf = ox.routing.route_to_gdf(hk_drive_data, astar_route)
    m = folium.Map(
        location=center_coords, 
        zoom_start=13,
        tiles="Esri World Street Map", 
        attr="Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS, Intermap, iPC, NRCAN, Esri Japan, METI, Esri China (Hong Kong), Esri (Thailand), TomTom, 2012"
    )
    for _, row in route_gdf.iterrows():
        folium.PolyLine([(lat, lon) for lon, lat in row.geometry.coords], color="red", weight=4).add_to(m)

    m.save("outputs/route_phase1.html")

if __name__ == "__main__":
    asyncio.run(main())