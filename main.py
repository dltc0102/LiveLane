"""
Program Purpose: A* routing engine that finds the fastest path from point A to point B.
"""

import os, json, httpx, asyncio, folium, math, shutil, pyogrio, time, functools, inspect
import osmnx as ox
import networkx as nx
from osmnx import geocoder as oxgeo
from urllib.parse import quote
from haversine import haversine
import pandas as pd
import geopandas as gpd
import xml.etree.ElementTree as ET # for converting xml extracted raw text into a python object

NodeID = int
EdgeKey = int
CoordDict = dict[str, float]

def timer_decorator(func):
    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        time_elapsed = time.perf_counter() - start_time
        print(f"'{func.__name__}' completed in {time_elapsed:.4f}s")
        return result

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        elapsed = time.perf_counter() - start_time
        print(f"'{func.__name__}' completed in {elapsed:.4f}s")
        return result

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper

@timer_decorator
def clear_dir(dirpath: str) -> None:
    if os.path.exists(dirpath):
        shutil.rmtree(dirpath)
    os.makedirs(dirpath)

@timer_decorator
def get_cartesian(details: CoordDict) -> tuple[float, float, float]:
    lat_r: float = math.radians(details['lat'])
    lon_r: float = math.radians(details['lon'])
    x: float = math.cos(lat_r) * math.cos(lon_r)
    y: float = math.cos(lat_r) * math.sin(lon_r)
    z: float = math.sin(lat_r)
    return x, y, z

@timer_decorator
def to_latlon(x: float, y: float, z: float) -> tuple[float, float]:
    lon: float = math.atan2(y, x)
    lat: float = math.atan2(z, math.sqrt(x*x + y*y))
    return math.degrees(lat), math.degrees(lon)

@timer_decorator
def calculate_center(start: CoordDict, end: CoordDict) -> tuple[float, float]:
    p1: tuple[float, float, float] = get_cartesian(start)
    p2: tuple[float, float, float] = get_cartesian(end)
    x, y, z = [(a + b) / 2 for a, b in zip(p1, p2)]
    norm: float = math.sqrt(x*x + y*y + z*z)
    return to_latlon(x/norm, y/norm, z/norm)

@timer_decorator
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
    def __init__(self, client: httpx.AsyncClient, cache_path: str = "./data/hongkong_drive.graphml"):
        self.penalty_seconds: int = 30
        self.congestion_factor: float = 1.1
        self.toll_penalty_seconds: int = 60

        self.client: httpx.AsyncClient  = client
        self.cache_path: str            = cache_path
        self.graph: nx.MultiDiGraph     = self.load_graph()
        self.cached_traffic: dict[str, float] | None = None

        self.apply_static_penalties()
        self.enforce_physical_limits()

    @timer_decorator
    def download_osmnx_data(self, network_origin: str = "Hong Kong", modal: str = "drive"):
        custom_filter = (
            '["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"]'
            '["area"!~"yes"]'
            '["motor_vehicle"!~"no"]'
            '["motorcar"!~"no"]'
            '["access"!~"private"]'
            '["route"!~"ferry"]'
        )
        data = ox.graph.graph_from_place(network_origin, network_type=modal, custom_filter=custom_filter)
        data = ox.add_edge_speeds(data)
        data = ox.add_edge_travel_times(data)
        return data

    @timer_decorator
    def load_graph(self) -> nx.MultiDiGraph:
        if os.path.exists(self.cache_path):
            driving_data = ox.load_graphml(self.cache_path)
        else:
            driving_data = self.download_osmnx_data()
            ox.save_graphml(driving_data, filepath=self.cache_path)

        driving_data.remove_edges_from(nx.selfloop_edges(driving_data))

        edges_to_remove = []
        for u, v, k, data in driving_data.edges(keys=True, data=True):
            highway = data.get('highway', '')
            route = data.get('route', '')
            if isinstance(highway, list): highway = " ".join(highway)
            if isinstance(route, list): route = " ".join(route)

            if any(bad in highway.lower() for bad in ['steps', 'footway', 'path', 'pedestrian', 'cycleway']):
                edges_to_remove.append((u, v, k))
            elif 'ferry' in route.lower():
                edges_to_remove.append((u, v, k))
        driving_data.remove_edges_from(edges_to_remove)
        print(f"Purged {len(edges_to_remove)} non-driving edges.")

        try:
            driving_data = ox.truncate.largest_component(driving_data, strongly=False)
        except AttributeError:
            driving_data = ox.utils_graph.get_largest_component(driving_data, strongly=False)
            
        print(f"Largest weakly connected component kept. Nodes: {len(driving_data.nodes)}, Edges: {len(driving_data.edges)}")

        if driving_data.graph.get('crs') != 'epsg:4326':
            driving_data = ox.project_graph(driving_data, to_crs='epsg:4326')
            
        return driving_data

    @timer_decorator 
    def apply_static_penalties(self) -> None:
        tl_count = 0
        toll_count = 0
        
        for u, v, k, edge_data in self.graph.edges(keys=True, data=True):
            # general congestion
            if 'travel_time' in edge_data:
                edge_data['travel_time'] *= self.congestion_factor
                
            # traffic light penalty
            if self.graph.nodes[v].get('highway') == 'traffic_signals' or self.graph.nodes[v].get('traffic_signals') == 'yes':
                edge_data['travel_time'] += self.penalty_seconds
                tl_count += 1
                
            # toll penalty
            is_toll = edge_data.get('toll') in [True, 'yes', 'true', '1']
            if is_toll:
                edge_data['travel_time'] += self.toll_penalty_seconds
                toll_count += 1

        print(f"Applied {self.congestion_factor}x congestion factor.")
        print(f"Added {self.penalty_seconds}s penalty to {tl_count} traffic lights.")
        print(f"Added {self.toll_penalty_seconds}s penalty to {toll_count} tolls.")

    @timer_decorator
    def enforce_physical_limits(self) -> None:
        edges_to_remove = []
        
        for u, v, k, edge_data in self.graph.edges(keys=True, data=True):
            length = edge_data.get("length", 0)
            if length <= 0:
                edges_to_remove.append((u, v, k))
                continue
                
            max_speed_m_s = 100 / 3.6
            min_time = length / max_speed_m_s
            current_time = edge_data.get("travel_time", 0)
            if current_time < min_time:
                edge_data["travel_time"] = min_time
                
        self.graph.remove_edges_from(edges_to_remove)
        if edges_to_remove:
            print(f"Removed {len(edges_to_remove)} physically impossible (zero-length) edges.")

    def create_heuristics(self, current_node: NodeID , target_node: NodeID ) -> float:
        lat_current: float = self.graph.nodes[current_node]['y']
        lon_current: float = self.graph.nodes[current_node]['x']
        lat_target: float = self.graph.nodes[target_node]['y']
        lon_target: float = self.graph.nodes[target_node]['x']
        dist_km: float = haversine((lat_current, lon_current), (lat_target, lon_target))
        max_speed: int = 80
        return (dist_km / max_speed) * 3600

    @timer_decorator
    def get_astar_route(self, start_coords: CoordDict, end_coords: CoordDict) -> list[NodeID]:
        start_node: NodeID = ox.distance.nearest_nodes(self.graph, X=start_coords['lon'], Y=start_coords['lat'])
        end_node: NodeID = ox.distance.nearest_nodes(self.graph, X=end_coords['lon'], Y=end_coords['lat'])

        # --- DIAGNOSTIC ---
        print(f"START NODE: {start_node} at ({self.graph.nodes[start_node]['y']}, {self.graph.nodes[start_node]['x']})")
        print(f"END NODE:   {end_node} at ({self.graph.nodes[end_node]['y']}, {self.graph.nodes[end_node]['x']})")
        print(f"GRAPH SIZE: {len(self.graph.nodes)} nodes, {len(self.graph.edges)} edges")
        # ------------------

        route = nx.astar_path(
            self.graph, 
            start_node, 
            end_node, 
            heuristic=self.create_heuristics, 
            weight="travel_time"
        )

        # --- DIAGNOSTIC: print each edge on the route ---
        print("ROUTE EDGES (first 20):")
        for i in range(min(20, len(route) - 1)):
            u, v = route[i], route[i+1]
            edge_data = self.graph[u][v][0]  # first key
            hw = edge_data.get('highway', 'unknown')
            length = edge_data.get('length', 0)
            tt = edge_data.get('travel_time', 0)
            print(f"  {u} -> {v}: hw={hw}, length={length:.1f}m, time={tt:.1f}s")
        # ---------------------------------------------

        return route

    @timer_decorator
    def get_route_stats(self, route: list[NodeID]) -> tuple[float, float]:
        route_gdf = ox.routing.route_to_gdf(self.graph, route)
        total_time = route_gdf["travel_time"].sum()
        total_distance = route_gdf["length"].sum()
        return total_time, total_distance

    @timer_decorator
    def parse_traffic_xml(self, xml_text: str) -> dict[str, float]:
        traffic_speeds: dict[str, float] = {}
        root = ET.fromstring(xml_text)

        for segment in root.findall(".//segment"):
            segment_id_node = segment.find("segment_id")
            valid_node = segment.find("valid")
            if segment_id_node is not None and valid_node is not None and valid_node.text == 'Y':
                speed_node = segment.find("speed")
                if speed_node is not None:
                    try:
                        speed = float(speed_node.text)
                        if speed > 0:
                            traffic_speeds[segment_id_node.text] = speed
                    except ValueError:
                        pass
        return traffic_speeds

    @timer_decorator
    async def apply_live_traffic(self, gdb_path: str = "./data/Road_Network_FGDB/Transportation_TNM.gdb") -> None:
        live_traffic_url = "https://resource.data.one.gov.hk/td/traffic-detectors/irnAvgSpeed-all.xml"
        
        try:
            response: httpx.Response = await self.client.get(live_traffic_url)
            response.raise_for_status()
            xml_text: str = response.text
            traffic_speeds = self.parse_traffic_xml(xml_text)
            self.cached_traffic = traffic_speeds
            print(f"{len(traffic_speeds)} valid live traffic speed records")

        except Exception as e:
            if self.cached_traffic:
                traffic_speeds = self.cached_traffic
                print(f'using cached traffic: {len(traffic_speeds)} records')
            else: 
                print('no cached traffic, using osmnx free-flow speeds')
                return
    
        mapping = self.preprocess_sjoin(gdb_path)

        count = 0
        for row in mapping.itertuples(index=False):
            route_id = row.ROUTE_ID
            if route_id in traffic_speeds:
                u, v, k = row.u, row.v, row.key
                if not self.graph.has_edge(u, v, k): continue
                length = self.graph[u][v][k]['length']
                live_speed = traffic_speeds[route_id]
                new_time = length / (live_speed / 3.6)
                if self.graph.nodes[v].get('highway') == 'traffic_signals':
                    new_time += self.penalty_seconds
                if self.graph[u][v][k].get('toll') in [True, 'yes', 'true', '1']:
                    new_time += self.toll_penalty_seconds
                self.graph[u][v][k]['travel_time'] = new_time
                count += 1
        print(f"Updated {count} OSM edges with live traffic data.")

    @timer_decorator
    def preprocess_sjoin(self, gdb_path: str, mapping_path: str = "./data/traffic_mapping.pkl") -> pd.DataFrame:
        if os.path.exists(mapping_path):
            return pd.read_pickle(mapping_path)

        gov_gdf = gpd.read_file(gdb_path, layer="CENTERLINE")
        route_id_col = 'ROUTE_ID' if 'ROUTE_ID' in gov_gdf.columns else 'route_id'
        gov_gdf[route_id_col] = gov_gdf[route_id_col].astype(str)
        gov_gdf = gov_gdf.rename(columns={route_id_col: 'ROUTE_ID'})

        nodes, edges = ox.graph_to_gdfs(self.graph)
        edges_proj = edges.reset_index().to_crs(epsg=2326)
        matched = gpd.sjoin_nearest(edges_proj, gov_gdf[['ROUTE_ID', 'geometry']], how="inner", distance_col="dist")
        mapping = matched[matched['dist'] < 30][['u', 'v', 'key', 'ROUTE_ID']].copy()
        mapping.to_pickle(mapping_path)
        return mapping

async def main() -> None:
    clear_dir("outputs")
    USER_HEADERS: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/xml, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
        "Connection": "keep-alive",
    }
    async with httpx.AsyncClient(headers=USER_HEADERS, timeout=15.0) as myclient:
        engine: RoutingEngine = RoutingEngine(client=myclient)
        await engine.apply_live_traffic()

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