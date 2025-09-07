# Program goal: Find the top N fastest routes between two points

import os, fiona, json, requests, re, time, googlemaps, heapq, shutil, ast, urllib.request, warnings, zipfile, glob
import shapely.geometry as geometry
from shapely.geometry import MultiLineString
from pyproj import Transformer
from bs4 import BeautifulSoup
import geopandas as gpd
from scipy.spatial import KDTree
import numpy as np
from colorama import Fore as CFore
from colorama import Style as CStyle
import haversine.haversine as haversine_calc
from tqdm import tqdm

start_time = time.time()
def timer_decorator(start_time):
    def decorator(func):
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            print(f"Function '{func.__name__}' completed in {elapsed:.4f}s")
            return result
        return wrapper
    return decorator

# remove weird pyogrio warnings
warnings.filterwarnings(
    "ignore",
    message="Measured.*geometry types are not supported",
    category=UserWarning,
    module="pyogrio.raw"
)


"""----------
    UTILS
----------"""
@timer_decorator(start_time)
def remove_filepath(filepath: str) -> None:
    if os.path.exists(filepath):
        if os.path.isfile(filepath):
            os.remove(filepath)
            print(f"Removed filepath '{filepath}'.")
        if os.path.isdir(filepath):
            shutil.rmtree(filepath)
            print(f"Removed directory '{filepath}'.")

def color_msg(color: str, msg: str) -> str:
    colors = {
        'red': CFore.RED,
        'blue': CFore.BLUE,
        'purple': CFore.MAGENTA,
        'cyan': CFore.CYAN,
        'green': CFore.GREEN,
        'yellow': CFore.YELLOW,
        'black': CFore.BLACK,
        'white': CFore.WHITE
    }
    return f"{colors[color.lower()]}{msg}{CStyle.RESET_ALL}"

@timer_decorator(start_time)
def get_gm_api_key() -> str:
    with open("apikey.txt", 'r', encoding='utf-8') as key_f:
        print("GM Api Key Received.")
        return key_f.readline().strip()

@timer_decorator(start_time)
def get_filepath_by_extension(extension: str) -> str:
    dataset_fp = os.path.join(os.getcwd(), 'dataset')
    for file in os.listdir(dataset_fp):
        if file.endswith(extension):
            return os.path.join(dataset_fp, file)


"""-------------------
   COORDINATE STUFF
-------------------"""
@timer_decorator(start_time)
def osm_address_lookup(request_address: str) -> json:
    NOMINATIM_HEADERS = {
        "User-Agent": "Roadblocker/1.0 (daniellautc@gmail.com)"
    }
    url = "https://nominatim.openstreetmap.org/search"

    params = {
        'q': request_address,
        'format': 'jsonv2',
        'limit': 5,
        'addressdetails': 1,
        'country_code': 'cn'
    }

    response = requests.get(url, params=params, headers=NOMINATIM_HEADERS)
    if response.status_code == 429:
        print("Rate limited by Nominatim. Please wait before making another request.")
        return None

    if response.status_code != 200:
        response.raise_for_status()
        return None

    return response.json()

def get_simplified_options(options: list | dict) -> list:
    if isinstance(options, list):
        simplified_options = []
        for option in options:
            option_details = {
                "place_id": option["place_id"],
                "display_name": option["display_name"],
                "category": option["category"],
                "type": option["type"],
                "lat": option["lat"],
                "lon": option["lon"],
            }
            simplified_options.append(option_details)
        return simplified_options
    elif isinstance(options, dict):
        return {
                "place_id": options["place_id"],
                "display_name": options["display_name"],
                "category": options["category"],
                "type": options["type"],
                "lat": options["lat"],
                "lon": options["lon"],
            }

def get_chosen_option(options: list) -> dict:
    simplified_options = get_simplified_options(options)
    chosen_idx = 1
    for idx, option in enumerate(simplified_options, 1):
        print(f"{idx}. {option["display_name"]} ({option["place_id"]})")
        for key, value in option.items():
            if key != "display_name" and key != "place_id":
                print(f" | {key}: {value}")
        print()

    print()
    while True:
        user_input = input(f"Which of the {len(simplified_options)} are you choosing? ")
        if user_input.isdigit():
            chosen_idx = int(user_input)
            result = options[chosen_idx]
            return options[chosen_idx]

def get_coordinate_details(start_details: dict, end_details: dict):
    return {
        "start": (float(start_details["lat"]), float(start_details["lon"])),
        "end": (float(end_details["lat"]), float(end_details["lon"])),
    }


"""----------
   OSM GDF
----------"""
warnings.filterwarnings("ignore", message="Non closed ring detected", category=RuntimeWarning, module="pyogrio.raw")

@timer_decorator(start_time)
def get_pbf_filepath() -> str:
    file_to_parse = None
    dataset_dirpath = os.path.join(os.getcwd(), 'dataset')
    for filepath in os.listdir(dataset_dirpath):
        if filepath.startswith('hong-kong') and filepath.endswith('.pbf'):
            file_to_parse = os.path.join(dataset_dirpath, filepath)
    print("Found PBF file to parse.")
    return file_to_parse

@timer_decorator(start_time)
def get_all_gdf_data(filepath: str) -> gpd.geodataframe.GeoDataFrame:
    if filepath.endswith('.pbf'):
        return gpd.read_file(filename=filepath, layer='lines', columns=["osm_id", "name", "highway", "other_tags", "geometry"])
    if filepath.endswith('.gdb'):
        return gpd. read_file(filename=filepath, layer='CENTERLINE', columns=["STREET_ENAME", "STREET_CNAME", "SHAPE_Length", "geometry"])

class OSM_GDF:
    def __init__(self, pbf_gdf_lines, api_key, headers, gov_gdf, expressway_limits, start_time):
        self.pbf_gdf_lines = pbf_gdf_lines
        self.api_key = api_key
        self.headers = headers
        self.gov_gdf = gov_gdf
        self.expressway_limits = expressway_limits
        self.start_time = start_time
        self.crs_transformer = Transformer.from_crs("EPSG:2326", "EPSG:4326", always_xy=True)

        self.lines_nodes = []
        self.lines_nodes_ways = []
        self.lines_ways_data = {}
        if not self.pbf_gdf_lines.empty:
            self.create_line_nodes()

        print(f"# of line nodes {len(self.lines_nodes)}\n")

        if self.lines_nodes_ways:
            self.lines_ways_data = self.get_line_nodes_ways()

    @timer_decorator(start_time)
    def create_line_nodes(self):
        print("Creating Line Nodes...")
        for idx, line in tqdm(self.pbf_gdf_lines.iterrows(), total=len(self.pbf_gdf_lines), desc="Creating line nodes"):
            if line["name"] is not None:
                line_node = OSM_LineNode(
                    self.api_key, self.headers, self.gov_gdf, self.expressway_limits, self.crs_transformer, line["osm_id"], line["name"], line["highway"], line["other_tags"], line["geometry"]
                )
                self.lines_nodes.append(line_node.get_node_data())
                self.lines_nodes_ways.append(line_node.get_way_data())
            if idx == 51:
                break
        print("All Line Nodes Created.")

    def get_line_nodes_ways(self) -> dict:
        """ returns a dict of each node

        Key:
            mid point of the road segment

        Value:
            0: first coords
            1: last coords
            2: street ename
            3: weight of the road segment

        """
        restructed_dict = {}
        for way_data in self.lines_nodes_ways:
            for way_key, way_value in way_data.items():
                street_name = way_key
                first_coords = way_value['first']
                last_coords = way_value['last']
                weight = way_value['weight']
                mid_coords = way_value['midpoint']

                connected_roads = self.query_connected_roads(mid_coords)
                is_dead_end = True if connected_roads == [] else False
                restructed_dict[mid_coords] = {
                    'first': first_coords,
                    'last': last_coords,
                    'name': street_name,
                    'weight': weight,
                    'connected_to': connected_roads,
                    'is_dead_end': is_dead_end
                }
        return restructed_dict

    def query_connected_roads(self, queried_point: tuple[float, float]) -> list:
        is_queried_point_in_dkeys = any(filter(lambda d: queried_point in d, self.lines_nodes_ways))
        if is_queried_point_in_dkeys:
            return [d[queried_point] for d in self.lines_nodes_ways if queried_point in d]
        return []

    def get_line_nodes(self) -> list:
        """ Returns a list of line_nodes"""
        return self.lines_nodes

    def get_lines_ways_data(self) -> dict:
        return self.lines_ways_data

class OSM_LineNode:
    def __init__(self, api_key, headers, gov_gdf, expressway_limits, crs_transformer, osm_id: int, name: str, highway: str, other_tags, geometry):
        self.api_key = api_key
        self.headers = headers
        self.gov_gdf = gov_gdf
        self.expressway_limits = expressway_limits
        self.crs_transformer = crs_transformer

        self.osm_id = osm_id
        self.name = name
        self.highway = highway
        self.other_tags = other_tags
        self.other_tags_data = {}
        self.geometry = geometry
        self.coord_lst = self.get_coords()
        self.first_coords = self.coord_lst[0]
        self.last_coords = self.coord_lst[-1]
        self.mid_coords = self.get_midpoint_coord()
        self.line_distance_km = haversine_calc(self.first_coords, self.last_coords)
        self.maxspeed = 0.0
        self.avespeed = 0.0
        self.weight = 0.0

        if self.other_tags:
            self.other_tags_data = self.get_other_tags_dict()

        self.en_name = self.other_tags_data.get("name:en", None)
        self.zh_name = self.other_tags_data.get("name:zh", None)
        self.minspeed = self.other_tags_data.get("minspeed", None)
        self.maxspeed = self.other_tags_data.get("maxspeed", None)
        self.is_oneway = self.other_tags_data.get("oneway", None)
        self.is_street = True if self.other_tags_data.get("street") == 'yes' else False
        self.lanes = self.other_tags_data.get("lanes", None)

        if self.maxspeed == 0.0 or not self.maxspeed:
            self.maxspeed = self.get_expressway_speed()

        if not isinstance(self.maxspeed, float):
            self.maxspeed = float(self.maxspeed)
        else:
            self.avespeed = self.maxspeed * 0.9

        while self.first_coords == self.last_coords:
            self.line_distance_km = self.query_gov_gdf(self.name)

        if self.avespeed != 0.0 and self.line_distance_km is not None:
            self.weight = self.line_distance_km / self.avespeed

    def convert_mls(self, giv_geometry) -> list[tuple[float, float]]:
        """ Converts a MultiLineString of EPSG:2326 Coordinates into a list of coordinates in WGS84 Format """
        if isinstance(giv_geometry, MultiLineString):
            all_coords = []
            for line_string in giv_geometry.geoms:
                for coord in line_string.coords:
                    eing, ning = coord
                    if eing is not None and ning is not None:
                        lon, lat = self.crs_transformer.transform(eing, ning)
                        all_coords.append((lat, lon))
            return all_coords

    def query_gov_gdf(self, address: str) -> float:
        """ Find street in government data and return its length in km. """
        for idx, street in self.gov_gdf.iterrows():
            street_ename = street["STREET_ENAME"]
            if street_ename and address.lower() in street_ename.lower():
                shape_length = street["SHAPE_Length"]
                if shape_length is not None:
                    return float(shape_length / 1000)

                self.coord_lst = self.convert_mls(street["geometry"])
                self.first_coords = self.coord_lst[0]
                self.last_coords = self.coord_lst[-1]
                h_dis = haversine_calc(self.first_coords, self.last_coords)
                return float(h_dis)

    def get_expressway_speed(self) -> float:
        """ Gets speed limit based on whether street's english name is in self.expressway_limits or not. """
        if self.en_name not in self.expressway_limits:
            return 50.0
        return self.expressway_limits[self.en_name]

    def get_other_tags_dict(self) -> dict:
        """ Parses the other_tags attribute into a dictionary for ease of access. """
        tag_content = self.other_tags.strip()
        tag_content = re.sub(r'\s+', ' ', tag_content)
        tag_dict = '{' + tag_content.replace('=>', ':') + '}'
        parsed_tag_dict = ast.literal_eval(tag_dict)

        result = {}
        for key, value in parsed_tag_dict.items():
            if isinstance(value, (int, float)):
                result[key] = float(value)
            else:
                result[key] = value
        return result

    def _print(self):
        print(f"\n{self.name} - [{self.first_coords} ~ {self.last_coords}]")
        print(f"distance: {self.line_distance_km}km")
        for key, value in self.other_tags_data.items():
            print(f"Tag '{key}': {value} {type(value)}")

    def get_node_data(self) -> dict:
        return {
            "osm_id": self.osm_id,
            "name": self.name,
            "en_name": self.en_name,
            "zh_name": self.zh_name,
            "is_highway": self.highway,
            "is_oneway": self.is_oneway,
            "is_street": self.is_street,
            "coords_lst": self.coord_lst,
            "first_coords": self.first_coords,
            "last_coords": self.last_coords,
            "node_length_km": self.line_distance_km,
            "lanes": self.lanes,
            "maxspeed": self.maxspeed,
            "minspeed": self.minspeed,
            "avespeed": self.avespeed,
            "weight": self.weight,
        }

        # print(f"{self.name} {self.first_coords} { self.last_coords} [{self.line_distance_km}]")

    def get_way_data(self) -> dict:
        """ Returns way data: {en_name: {first: first_coords, last: last_coords, weight: weight}} """
        return {self.en_name: {'first': self.first_coords, 'last': self.last_coords, 'midpoint': self.mid_coords, 'weight': self.weight}}

    def get_coords(self) -> list[tuple[float, float]]:
        """ Takes geometry and converts it into a list of coordinates. (assumes geometry is already in wgs84 format) """
        coords = []
        for coord in self.geometry.coords:
            lon, lat = coord
            result = (lat, lon)
            coords.append(result)
        return coords

    def get_midpoint_coord(self) -> tuple[float, float]:
        start_lat, start_lon = self.first_coords
        end_lat, end_lon = self.last_coords
        mid_lat = (start_lat + end_lat) / 2
        mid_lon = (start_lon + end_lon) / 2
        return (mid_lat, mid_lon)

"""----------""
   GOV GDF
----------"""
@timer_decorator(start_time)
def get_latest_road_network(dataset_filepath: str) -> str:
    filename = "RdNet_IRNP.gdb"
    gdb_filepath = os.path.join(dataset_filepath, filename)

    if os.path.exists(gdb_filepath):
        print("Road network dataset already exists, redownload is not needed.")
        return gdb_filepath

    download_url: str = "https://static.data.gov.hk/td/road-network-v2/RdNet_IRNP.gdb.zip"
    os.makedirs(dataset_filepath, exist_ok=True)

    dataset_zip_path: str = os.path.join(dataset_filepath, "dataset.zip")
    urllib.request.urlretrieve(download_url, dataset_zip_path)

    with zipfile.ZipFile(dataset_zip_path, 'r') as zip_f:
        zip_f.extractall(dataset_filepath)

    os.remove(dataset_zip_path)
    print("Road network dataset downloaded and extracted.")
    return gdb_filepath

@timer_decorator(start_time)
def get_expressway_limits(headers: dict) -> dict | None:
    url = "https://en.wikipedia.org/wiki/List_of_streets_and_roads_in_Hong_Kong"
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    if res.status_code != 200:
        print("url not 200.")
        return None

    soup = BeautifulSoup(res.content, "html.parser")
    expressway_table = soup.select_one("table.wikitable.collapsible")
    expressway_rows = expressway_table.find_all("tr")[1:]

    expressway_limits = {}
    for row in expressway_rows:
        cells = row.find_all(['td', 'th'])

        if len(cells) >= 3:
            name_cell = cells[0]
            name_link = name_cell.find('a', title=True)
            expressway_name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)
            speed_text = cells[2].get_text(strip=True)
            numbers = re.findall(r'\d+', speed_text)
            if numbers:
                if len(numbers) == 1:
                    speed_limit = float(numbers[0])
                else:
                    speed_limit = float(max(numbers))

                expressway_limits[expressway_name] = speed_limit

    return expressway_limits

class initial_KDTree:
    def __init__(self, query_points, lines_data, osm_gdf, gov_gdf):
        self.start_point: tuple[float, float] = query_points['start']
        self.target_point: tuple[float, float] = query_points['target']
        self.lines_data     : dict = lines_data
        self.osm_gdf        : OSM_GDF = osm_gdf
        self.gov_gdf        : gpd.geodataframe.GeoDataFrame = gov_gdf

        self.all_coord_points: list = self.get_all_coords()
        self.kdtree: KDTree = KDTree(self.all_coord_points)
        print(self.all_coord_points, len(self.all_coord_points))

        if self.kdtree:
            self.get_initial_nearest_neighbors()

    def get_initial_nearest_neighbors(self):
        distances, indices = self.kdtree.query(self.start_point, k=5)
        for dists, idx in zip(distances, indices):
            print(dists, idx)

    def get_all_coords(self) -> list[tuple]:
        all_coords = []
        # key: midpoint
        # value: first, last, name, weight, connected_to, is_dead_end
        for way_key, way_value in self.lines_data.items():
            first_coord = way_value["first"]
            last_coord = way_value["last"]
            all_coords.append(first_coord)
            all_coords.append(last_coord)
        return all_coords

def main():
    USER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }
    gm_api_key          : str = get_gm_api_key()
    output_fp           : str = os.path.join(os.getcwd(), 'output')
    expressway_limits   : dict = get_expressway_limits(USER_HEADERS)

    pbf_file_path       : str = get_filepath_by_extension('.pbf')
    pbf_gdf_lines = get_all_gdf_data(pbf_file_path)

    gov_gdf_path = get_filepath_by_extension('.gdb')
    gov_gdf = get_all_gdf_data(gov_gdf_path)

    osm_gdf             : OSM_GDF = OSM_GDF(pbf_gdf_lines, gm_api_key, USER_HEADERS, gov_gdf, expressway_limits, start_time)
    osm_lines_ways_data : dict = osm_gdf.get_lines_ways_data()

    # sample_query = {
    #     'start': (22.321584526395938, 113.93946781536052),
    #     'target': (22.2853336, 114.1330190)
    # }

    # init_tree = initial_KDTree(sample_query, osm_lines_ways_data, osm_gdf, gov_gdf)


    # dev

    # address look up
    start_address: str = "2 lung pak street"
    end_address: str = "89 pok fu lam road"
    # osm_start_options: json = osm_address_lookup(request_address=start_address)
    # osm_end_options: json = osm_address_lookup(request_address=end_address)
    # osm_start: dict = get_chosen_option(osm_start_options)
    # time.sleep(2)
    # osm_end: dict = get_chosen_option(osm_end_options)
    # coordinate_details: dict = get_coordinate_details(osm_start, osm_end)

    # hardcode coordinate details for now
    # coordinate_details = {
    #     "start": [22.3642146, 114.1794265],
    #     "end": [22.2853336, 114.1330190]
    # }

    # algorithm



if __name__ == "__main__":
    main()