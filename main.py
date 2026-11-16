"""
Program Purpose: A* routing engine that finds the fastest path from point A to point B.
"""

import os, json, httpx, asyncio, folium, math, shutil
import osmnx as ox
import networkx as nx
from osmnx import geocoder as oxgeo
from urllib.parse import quote
from haversine import haversine
import pandas as pd
import geopandas as gpd

NodeID = int
EdgeKey = int
CoordDict = dict[str, float]

def clear_dir(dirpath: str) -> None:
    if os.path.exists(dirpath):
        shutil.rmtree(dirpath)
    os.makedirs(dirpath)

def get_cartesian(details: CoordDict) -> tuple[float, float, float]:
    lat_r: float = math.radians(details['lat'])
    lon_r: float = math.radians(details['lon'])
    x: float = math.cos(lat_r) * math.cos(lon_r)
    y: float = math.cos(lat_r) * math.sin(lon_r)
    z: float = math.sin(lat_r)
    return x, y, z

def to_latlon(x: float, y: float, z: float) -> tuple[float, float]:
    lon: float = math.atan2(y, x)
    lat: float = math.atan2(z, math.sqrt(x*x + y*y))
    return math.degrees(lat), math.degrees(lon)

def calculate_center(start: CoordDict, end: CoordDict) -> tuple[float, float]:
    p1: tuple[float, float, float] = get_cartesian(start)
    p2: tuple[float, float, float] = get_cartesian(end)
    x, y, z = [(a + b) / 2 for a, b in zip(p1, p2)]
    norm: float = math.sqrt(x*x + y*y + z*z)
    return to_latlon(x/norm, y/norm, z/norm)

def generate_map(graph: nx.MultiDiGraph, route: list[NodeID], center_coords: tuple[float, float], output_path: str) -> None:
    route_gdf: gpd.GeoDataFrame = ox.routing.route_to_gdf(graph, route)
    
    m: folium.Map = folium.Map(
        location=center_coords, 
        zoom_start=13,
        tiles="Esri World Street Map", 
        attr="Tiles &copy; Esri"
    )
    
    for _, row in route_gdf.iterrows():
        folium.PolyLine(
            [(lat, lon) for lon, lat in row.geometry.coords], 
            color="red", 
            weight=4
        ).add_to(m)
        
    m.save(output_path)

class RoutingEngine:
    def __init__(self, cache_path: str = "./data/hongkong_drive.graphml", penalty_seconds: int = 25):
        self.cache_path: str = cache_path
        self.penalty_seconds: int = penalty_seconds
        self.graph: nx.MultiDiGraph = self.load_graph()
        self.apply_traffic_penalties()

    def download_osmnx_data(self, network_origin: str = "Hong Kong", modal: str = "drive"):
        data = ox.graph.graph_from_place(network_origin, network_type=modal)
        data = ox.add_edge_speeds(data)
        data = ox.add_edge_travel_times(data)
        return data
    
    def load_graph(self) -> nx.MultiDiGraph:
        if os.path.exists(self.cache_path):
            return ox.load_graphml(self.cache_path)
        driving_data = self.download_osmnx_data()
        ox.save_graphml(driving_data, filepath=self.cache_path)
        return driving_data
            
    def apply_traffic_penalties(self) -> None:
        count = 0
        for source_node_id, target_node_id, edge_key, edge_data in self.graph.edges(keys=True, data=True):
            if self.graph.nodes[target_node_id].get('highway') == 'traffic_signals':
                edge_data['travel_time'] += self.penalty_seconds
                count += 1

        print(f"Added {self.penalty_seconds}s penalty to {count} edges entering traffic lights.")

    def create_heuristics(self, current_node: NodeID , target_node: NodeID ) -> float:
        lat_current: float = self.graph.nodes[current_node]['y']
        lon_current: float = self.graph.nodes[current_node]['x']
        lat_target: float = self.graph.nodes[target_node]['y']
        lon_target: float = self.graph.nodes[target_node]['x']
        dist_km: float = haversine((lat_current, lon_current), (lat_target, lon_target))
        return (dist_km / 110) * 3600

    def get_astar_route(self, start_coords: CoordDict, end_coords: CoordDict) -> list[NodeID]:
        start_node: NodeID = ox.distance.nearest_nodes(self.graph, X=start_coords['lon'], Y=start_coords['lat'])
        end_node: NodeID = ox.distance.nearest_nodes(self.graph, X=end_coords['lon'], Y=end_coords['lat'])

        print("Calculating A* route...")
        return nx.astar_path(
            self.graph, 
            start_node, 
            end_node, 
            heuristic=self.create_heuristics, 
            weight="travel_time"
        )

    def get_route_stats(self, route: list[NodeID]) -> tuple[float, float]:
        route_gdf = ox.routing.route_to_gdf(self.graph, route)
        total_time = route_gdf["travel_time"].sum()
        total_distance = route_gdf["length"].sum()
        return total_time, total_distance
    
async def main() -> None:
    clear_dir("outputs")
    engine: RoutingEngine = RoutingEngine(penalty_seconds=25)

    # test details
    home_details: CoordDict = {'lat': 22.3638, 'lon': 114.1791}
    belchers_details: CoordDict = {'lat': 22.2860, 'lon': 114.1329}

    route: list[NodeID] = engine.get_astar_route(home_details, belchers_details)
    total_time, total_dist = engine.get_route_stats(route)
    print(f"Result: {len(route)} nodes, {total_time/60:.1f} min, {total_dist/1000:.2f} km")

    center_coords = calculate_center(home_details, belchers_details)
    generate_map(engine.graph, route, center_coords, "outputs/route_phase1.html")
    
if __name__ == "__main__":
    asyncio.run(main())