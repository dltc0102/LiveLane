import httpx, asyncio, pyproj, re, os, folium, json
import streamlit as st
import osmnx as ox
import networkx as nx
from dataclasses import dataclass
from difflib import SequenceMatcher
import streamlit.components.v1 as components
from shapely.geometry import LineString
from main import calculate_center


class AddressQuery:
    URL = "https://www.map.gov.hk/gs/api/v1.0.0/locationSearch"
    
    DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}

    HK_DISTRICTS = (
        "Kwai Tsing", "Islands", "North", "Sai Kung", "Sha Tin", "Tai Po",
        "Tsuen Wan", "Yuen Long", "Kowloon City", "Kwun Tong", "Sham Shui Po",
        "Wong Tai Sin", "Yau Tsim Mong", "Central and Western", "Eastern",
        "Southern", "Wan Chai",
    )

    STREET_TYPES = (
        r"(street|st|road|rd|avenue|ave|lane|ln|drive|dr|court|ct|place|pl|way|"
        r"crescent|cres|terrace|path|square|sq|circuit|highway|hwy|bypass|parade|"
        r"interchange|flyover)"
    )

    BUILDING_RE = re.compile(r"\b(block|building|tower|court|house|mansion|estate|garden[s]?|plaza|centre|center|complex|phase|villa|terrace)\b", re.IGNORECASE)

    UNIT_RE = re.compile(r"\b(tower|block|phase|building)\s*(\d+[a-z]?)\b", re.IGNORECASE)

    POI_HINT_RE = re.compile(r"\b(defibrillators?|taxi stands?|car parks?|toilets?|lifts?|escalators?|atms?|kiosks?|management offices?|clubhouses?|recreation clubs?|commercial complexes?|schools?|hospitals?)\b", re.IGNORECASE)

    NUMBERED_STREET_RE = re.compile(rf"\b(\d+[a-z]?)\s+([a-z][a-z\s]*?{STREET_TYPES})\b", re.IGNORECASE)

    TRANSFORMER = pyproj.Transformer.from_crs(2326, 4326, always_xy=True)

    @dataclass
    class Candidate:
        name: str
        address: str
        district: str
        lat: float
        lon: float
        score: float

        def display(self) -> str:
            bits = [self.name, self.address, self.district]
            return ", ".join(b for b in bits if b)

    @staticmethod
    def norm(s: str | None) -> str:
        if not s:
            return ""
        s = re.sub(r"['\u2018\u2019\u201C\u201D`\u00B4\|]", "", s)
        return re.sub(r"\s+", " ", s).strip().lower()

    @classmethod
    def tokens(cls, s: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", cls.norm(s)))

    @classmethod
    def split_street_number(cls, address_en: str) -> tuple[str, str | None]:
        a = cls.norm(address_en)
        m = re.match(r"^(\d+)(?:-\d+)?\s+(.+)$", a)
        if m:
            return m.group(2).strip(), m.group(1)
        return a, None

    @classmethod
    def score(cls, query: str, item: dict) -> float:
        q = cls.norm(query)
        q_tokens = cls.tokens(q)
        if not q_tokens:
            return 0.0

        name_en = cls.norm(item.get("nameEN", ""))
        addr_en = cls.norm(item.get("addressEN", ""))
        district_en = cls.norm(item.get("districtEN", ""))
        name_tokens = cls.tokens(name_en)
        addr_tokens = cls.tokens(addr_en)

        total = 0.0

        cand_street, cand_num = cls.split_street_number(item.get("addressEN", ""))
        numbered_street_match = cls.NUMBERED_STREET_RE.search(q)
        if numbered_street_match and cand_num:
            q_num, q_street = numbered_street_match.group(1), numbered_street_match.group(2).strip()
            if q_num == cand_num:
                total += 3.0
            total += 2.5 * SequenceMatcher(None, q_street, cand_street).ratio()

        building_match = cls.BUILDING_RE.search(q)
        if building_match:
            prefix_tokens = cls.tokens(q[: building_match.start()])
            if prefix_tokens and prefix_tokens.issubset(name_tokens | addr_tokens):
                total += 2.0
            elif building_match.group(0).lower() in (name_tokens | addr_tokens):
                total += 0.8

        unit_match = cls.UNIT_RE.search(q)
        if unit_match:
            unit_word, unit_num = unit_match.group(1).lower(), unit_match.group(2)
            if re.search(rf"\b{unit_word}\s*{unit_num}\b", name_en):
                total += 10.0
            elif re.search(rf"\b{unit_word}\b", name_en):
                total += 0.5

        for district in cls.HK_DISTRICTS:
            norm_district = cls.norm(district)
            if norm_district in q and norm_district in (name_en + " " + addr_en + " " + district_en):
                total += 1.0
                break

        if cls.POI_HINT_RE.search(name_en):
            total -= 3.0

        cand_tokens = name_tokens | addr_tokens
        total += 0.5 * len(q_tokens & cand_tokens) / max(1, len(q_tokens | cand_tokens))

        if q in name_en or q in addr_en:
            total += 2.0

        return total

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client: httpx.AsyncClient = client

    async def lookup(self, query: str, *, top_n: int = 5, min_score: float = 0.5) -> list["AddressQuery.Candidate"]:
        resp = await self.client.get(self.URL, params={"q": query})
        resp.raise_for_status()
        raw = resp.json()

        scored: list[tuple[float, dict]] = []
        for item in raw:
            if "x" not in item or "y" not in item:
                continue
            try:
                float(item["x"])
                float(item["y"])
            except (TypeError, ValueError):
                continue
            scored.append((self.score(query, item), item))

        scored.sort(key=lambda p: p[0], reverse=True)

        out: list["AddressQuery.Candidate"] = []
        for sc, item in scored[:top_n]:
            if sc < min_score:
                break
            lon, lat = self.TRANSFORMER.transform(float(item["x"]), float(item["y"]))
            out.append(self.Candidate(
                name=(item.get("nameEN") or item.get("nameZH") or "").strip(),
                address=(item.get("addressEN") or item.get("addressZH") or "").strip(),
                district=(item.get("districtEN") or item.get("districtZH") or "").strip(),
                lat=lat,
                lon=lon,
                score=sc,
            ))
        return out

    async def best(self, query: str, *, min_score: float = 0.5) -> "AddressQuery.Candidate | None":
        results = await self.lookup(query, top_n=1, min_score=min_score)
        return results[0] if results else None

    async def latlon(self, query: str, *, min_score: float = 0.5) -> tuple[float, float] | None:
        cand = await self.best(query, min_score=min_score)
        return (cand.lat, cand.lon) if cand else None

    @classmethod
    def latlon_blocking(cls, query: str, *, min_score: float = 0.5, timeout: float = 15.0) -> tuple[float, float] | None:
        async def _run() -> tuple[float, float] | None:
            async with httpx.AsyncClient(headers=dict(cls.DEFAULT_HEADERS), timeout=timeout) as client:
                return await cls(client).latlon(query, min_score=min_score)
        return asyncio.run(_run())

# cache and draw map
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")
LINE_STYLES = {
    "Solid": None,
    "Dashed": "10, 10",
    "Dotted": "2, 6",
    "Dash-dot": "10, 5, 2, 5",
    "Long dash": "20, 10",
    "Dash-dot-dot": "10, 5, 2, 5, 2, 5",
}


# functions
@st.cache_data(ttl=0, show_spinner=False)
def fetch_route_cached(start_c, end_c, avoid_lights, avoid_turns):
    payload = {
        "start": start_c,
        "end": end_c,
        "preferences": {"avoid_lights": avoid_lights, "avoid_turns": avoid_turns},
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(f"{API_URL}/route", json=payload)
        response.raise_for_status()
        return response.json()

@st.cache_data(ttl=300, show_spinner=False)
def lookup_sync(query: str, top_n: int = 8) -> list[tuple[str, float, float]]:
    async def _run() -> list[AddressQuery.Candidate]:
        async with httpx.AsyncClient(headers=dict(AddressQuery.DEFAULT_HEADERS), timeout=15.0) as client:
            return await AddressQuery(client).lookup(query, top_n=top_n, min_score=0.0)
    return [(c.display(), c.lat, c.lon) for c in asyncio.run(_run())]

@st.cache_resource
def get_map_graph():
    driving_data = ox.load_graphml("./data/hongkong_full.graphml")
    driving_data.remove_edges_from(nx.selfloop_edges(driving_data))
    
    keep = []
    for u, v, k, d in driving_data.edges(keys=True, data=True):
        hw = d.get('highway', '')
        if isinstance(hw, list): hw = ' '.join(hw)
        hw = hw.lower()
        drivable = ('motorway','motorway_link','trunk','trunk_link','primary','primary_link',
                    'secondary','secondary_link','tertiary','tertiary_link',
                    'unclassified','residential','living_street','service','road')
        if not any(t in hw for t in drivable):
            continue
        if d.get('motor_vehicle') in ('no', False): continue
        if d.get('motorcar') in ('no', False): continue
        if d.get('access') == 'private': continue
        if 'ferry' in str(d.get('route', '')).lower(): continue
        keep.append((u, v, k))

    driving_data = driving_data.edge_subgraph(keep).copy()
    try:
        driving_data = ox.truncate.largest_component(driving_data, strongly=True)
    except AttributeError:
        driving_data = ox.utils_graph.get_largest_component(driving_data, strongly=True)
    return driving_data

def draw_route_map(res: dict, start: dict, end: dict, map_layer: str, road_color: str, tunnel_color: str, walk_color: str, weight: int, road_style: str | None = None, tunnel_style: str | None = None, walk_style: str | None = None) -> folium.Map:
    center = calculate_center(start, end)
    if map_layer == "OpenStreetMap":
        tiles_url = "OpenStreetMap"
        attr = None
    elif map_layer == "Esri World Topographic":
        tiles_url = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
        attr = "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community"
    elif map_layer == "OpenTopoMap":
        tiles_url = "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png"
        attr = "Map data: &copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors, <a href='http://viewfinderpanoramas.org'>SRTM</a> | Map style: &copy; <a href='https://opentopomap.org'>OpenTopoMap</a> (<a href='https://creativecommons.org/licenses/by-sa/3.0/'>CC-BY-SA</a>)"
    else:
        tiles_url = "Esri World Imagery"
        attr = None

    map_visual = folium.Map(
        location=center,
        zoom_start=14,
        tiles=tiles_url,
        attr=attr
    )

    def is_close(c1, c2, tol=1e-7):
        return abs(c1[0] - c2[0]) < tol and abs(c1[1] - c2[1]) < tol

    current_coords = []
    current_is_tunnel = None

    for segment in res.get('route_geometry', []):
        is_tunnel = segment['is_tunnel']
        coords = segment['coords']
        if not coords: continue

        if len(coords) > 2:
            line = LineString([(c[1], c[0]) for c in coords])
            simplified_line = line.simplify(0.0001, preserve_topology=True)
            coords = [[pt[1], pt[0]] for pt in simplified_line.coords]

        if current_is_tunnel is None:
            current_is_tunnel = is_tunnel
            current_coords.extend(coords)
        elif is_tunnel == current_is_tunnel:
            if current_coords:
                if is_close(current_coords[-1], coords[0]):
                    current_coords.extend(coords[1:])
                elif is_close(current_coords[-1], coords[-1]):
                    current_coords.extend(coords[-2::-1])
                else:
                    color_to_use = tunnel_color if current_is_tunnel else road_color
                    dash = tunnel_style if current_is_tunnel else road_style
                    folium.PolyLine(current_coords, color=color_to_use, weight=weight, opacity=0.8, dash_array=dash).add_to(map_visual)
                    current_coords = coords
            else:
                current_coords.extend(coords)
        else:
            color_to_use = tunnel_color if current_is_tunnel else road_color
            dash = tunnel_style if current_is_tunnel else road_style
            folium.PolyLine(current_coords, color=color_to_use, weight=weight, opacity=0.8, dash_array=dash).add_to(map_visual)
            
            current_is_tunnel = is_tunnel
            current_coords = coords

    if current_coords:
        color_to_use = tunnel_color if current_is_tunnel else road_color
        dash = tunnel_style if current_is_tunnel else road_style
        folium.PolyLine(current_coords, color=color_to_use, weight=weight, opacity=0.8, dash_array=dash).add_to(map_visual)

    if res.get('start_walk_path'):
        folium.PolyLine(res['start_walk_path'], color=walk_color, weight=4, dash_array=walk_style, tooltip="Walk to start point").add_to(map_visual)
    if res.get('end_walk_path'):
        folium.PolyLine(res['end_walk_path'], color=walk_color, weight=4, dash_array=walk_style, tooltip="Walk to destination").add_to(map_visual)

    folium.Marker([start['lat'], start['lon']], icon=folium.Icon(color="green", icon="info-sign")).add_to(map_visual)
    folium.Marker([end['lat'],   end['lon']],   icon=folium.Icon(color="red",   icon="info-sign")).add_to(map_visual)

    bounds = [[min(start['lat'], end['lat']), min(start['lon'], end['lon'])],
              [max(start['lat'], end['lat']), max(start['lon'], end['lon'])]]
    map_visual.fit_bounds(bounds)
    return map_visual
    
def load_json(fpath: str) -> json:
    with open(fpath, "r") as f:
        return json.load(f)
    
def load_style_config(fpath: str) -> None:
    default_colors = {
        "map_layer": "Esri World Imagery",
        "sat_road_color": "#00FFFF",
        "sat_tunnel_color": "#FF9E9E",
        "sat_walk_color": "#00FF00",
        "osm_road_color": "#FF4F52",
        "osm_tunnel_color": "#263EFF",
        "osm_walk_color": "#B5FD9D",
        "sat_line_weight": 4,
        "osm_line_weight": 5,
        "road_style": "Solid",
        "tunnel_style": "Dashed",
        "walk_style": "Dashed",
    }
    if os.path.exists(fpath):
        try:
            saved = load_json(fpath)
            default_colors.update(saved)
            return default_colors
        except Exception:
            pass
    return default_colors

def save_style_config(config, fpath: str) -> None:
    with open(fpath, "w") as f:
        json.dump(config, f, indent=4)

def style_row(label: str, color_key: str, style_key: str):
    st.markdown(f"**{label}**")
    c1, c2 = st.columns(2)
    color = c1.color_picker("Color", style_config.get(color_key, "#000000"), key=f"cp_{color_key}", label_visibility="collapsed")
    style_options = list(LINE_STYLES.keys())
    style = c2.selectbox("Style", style_options, index=style_options.index(style_config.get(style_key, "Solid")), key=f"ls_{style_key}", label_visibility="collapsed")
    return color, style

# styling for sidebar
STYLING_CONFIG_FPATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_style_config.json")
style_config = load_style_config(STYLING_CONFIG_FPATH)

# ui
st.set_page_config(page_title="LiveLane", layout="wide")
st.title("LiveLane -- A* Route Engine for Hong Kong")
st.markdown("Find the fastest route using live TD traffic data and A* routing.")

for key, default in {"start_coords": None, "end_coords": None, "route_results": None, "map_html": None, "start_options": [], "end_options": [], "start_fetched": "", "end_fetched": "", "avoid_lights_ever_ticked": False, "avoid_turns_ever_ticked": False}.items():
    st.session_state.setdefault(key, default)

st.sidebar.header("Route Details")
start_query = st.sidebar.text_input("Start Address", placeholder="Enter starting address here...")
end_query = st.sidebar.text_input("End Address", placeholder="Enter target destination address here...")

if not start_query:
    st.session_state.start_options = []
    st.session_state.start_fetched = ""
elif start_query != st.session_state.start_fetched:
    st.session_state.start_options = lookup_sync(start_query)
    st.session_state.start_fetched = start_query
    st.session_state.start_coords = None
    st.session_state.route_results = None
    st.session_state.pop("start_sel", None)
    st.session_state.avoid_lights_ever_ticked = False
    st.session_state.avoid_turns_ever_ticked = False

if not end_query:
    st.session_state.end_options = []
    st.session_state.end_fetched = ""
elif end_query != st.session_state.end_fetched:
    st.session_state.end_options = lookup_sync(end_query)
    st.session_state.end_fetched = end_query
    st.session_state.end_coords = None
    st.session_state.route_results = None
    st.session_state.pop("end_sel", None)
    st.session_state.avoid_lights_ever_ticked = False
    st.session_state.avoid_turns_ever_ticked = False

if st.session_state.start_options:
    labels = [o[0] for o in st.session_state.start_options]
    sel = st.sidebar.selectbox("Select Start", labels, index=None, key="start_sel")
    if sel:
        _, lat, lon = st.session_state.start_options[labels.index(sel)]
        st.session_state.start_coords = {"lat": lat, "lon": lon}

if st.session_state.end_options:
    labels = [o[0] for o in st.session_state.end_options]
    sel = st.sidebar.selectbox("Select End", labels, index=None, key="end_sel")
    if sel:
        _, lat, lon = st.session_state.end_options[labels.index(sel)]
        st.session_state.end_coords = {"lat": lat, "lon": lon}


### UI 
st.sidebar.markdown("---")

# preferences collapsible
def trigger_route_recalc():
    if st.session_state.start_coords and st.session_state.end_coords:
        st.session_state.route_results = None
        st.session_state.map_html = None

with st.sidebar.expander("Routing Preferences", expanded=False):
    arrow_pointing_down: str = "↓"
    arrow_pointing_up: str = "↑"
    red_color_format: str = "<span style='color:#dc3545;font-size:14px;'>"
    green_color_format: str = "<span style='color:#28a745;font-size:14px;'>"
    gray_color_format: str = "<span style='color:gray;font-size:14px;'>"

    def format_time_delta(seconds):
        if abs(seconds) >= 60:
            return f"{abs(seconds)/60:.1f} min"
        return f"{abs(seconds):.0f} sec"

    # lights
    c1, c2 = st.columns([0.6, 0.4])
    with c1:
        st.checkbox("Avoid Traffic Lights", key="avoid_lights", help="Heavily penalizes intersections with traffic signals.")

    if st.session_state.avoid_lights:
        st.session_state.avoid_lights_ever_ticked = True
    with c2:
        delta = st.session_state.route_results.get('lights_delta', 0) if st.session_state.route_results else 0
        time_delta = st.session_state.route_results.get('lights_time_delta', 0) if st.session_state.route_results else 0
        time_str = format_time_delta(time_delta)
        if not st.session_state.avoid_lights_ever_ticked:
            st.markdown(f"{gray_color_format}- 0 lights<br>Time: -</span>", unsafe_allow_html=True)
        else:
            light_sign = "+" if delta >= 0 else "-"
            light_magnitude = abs(delta)
            if time_delta > 0:
                color = red_color_format
                arrow = arrow_pointing_down
                t_sign = "+"
            else:
                color = green_color_format
                arrow = arrow_pointing_up
                t_sign = "-"
            st.markdown(f"{color}{arrow} {light_sign}{light_magnitude} lights<br>Time: {t_sign}{time_str}</span>", unsafe_allow_html=True)

    # turns
    c3, c4 = st.columns([0.6, 0.4])
    with c3:
        st.checkbox("Prefer Straight Roads", key="avoid_turns", help="Penalizes short connector edges (frequent turns).")

    if st.session_state.avoid_turns:
        st.session_state.avoid_turns_ever_ticked = True
    with c4:
        delta = st.session_state.route_results.get('turns_delta', 0) if st.session_state.route_results else 0
        time_delta = st.session_state.route_results.get('turns_time_delta', 0) if st.session_state.route_results else 0
        time_str = format_time_delta(time_delta)
        if not st.session_state.avoid_turns_ever_ticked:
            st.markdown(f"{gray_color_format}- 0 turns<br>Time: -</span>", unsafe_allow_html=True)
        else:
            turn_sign = "+" if delta >= 0 else "-"
            turn_magnitude = abs(delta)
            if time_delta > 0:
                color = red_color_format
                arrow = arrow_pointing_down
                t_sign = "+"
            else:
                color = green_color_format
                arrow = arrow_pointing_up
                t_sign = "-"
            st.markdown(f"{color}{arrow} {turn_sign}{turn_magnitude} turns<br>Time: {t_sign}{time_str}</span>", unsafe_allow_html=True)

# Invalidate route result when preference widgets change
if "prev_avoid_lights" not in st.session_state:
    st.session_state.prev_avoid_lights = st.session_state.avoid_lights
if "prev_avoid_turns" not in st.session_state:
    st.session_state.prev_avoid_turns = st.session_state.avoid_turns

if (st.session_state.avoid_lights != st.session_state.prev_avoid_lights) or (st.session_state.avoid_turns != st.session_state.prev_avoid_turns):
    st.session_state.route_results = None
    st.session_state.map_html = None
    st.session_state.prev_avoid_lights = st.session_state.avoid_lights
    st.session_state.prev_avoid_turns = st.session_state.avoid_turns


# map styling collapsible
with st.sidebar.expander("Map Styling", expanded=False):
    # layers
    layer_options = ["Esri World Imagery", "OpenStreetMap", "Esri World Topographic", "OpenTopoMap"]
    map_layer = st.selectbox(
        "Map Base Layer", 
        layer_options, 
        index=layer_options.index(style_config.get("map_layer", "Esri World Imagery")),
        key="map_layer_select",
        on_change=trigger_route_recalc
    )
    
    st.markdown("---")
    
    # color picker for layers
    prefix = "osm" if map_layer in ["OpenStreetMap", "Esri World Topographic", "OpenTopoMap"] else "sat"
    
    road_color, road_style = style_row("Road", f"{prefix}_road_color", "road_style")
    tunnel_color, tunnel_style = style_row("Tunnel/Bridge", f"{prefix}_tunnel_color", "tunnel_style")
    walk_color, walk_style = style_row("Walking", f"{prefix}_walk_color", "walk_style")
    line_weight = st.slider("Line Thickness", 2, 12, style_config.get(f"{prefix}_line_weight", 4))

    if st.button("Save Styles", key="save_styles_btn"):
        style_config["map_layer"] = map_layer
        style_config[f"{prefix}_road_color"] = road_color
        style_config[f"{prefix}_tunnel_color"] = tunnel_color
        style_config[f"{prefix}_walk_color"] = walk_color
        style_config[f"{prefix}_line_weight"] = line_weight
        style_config["road_style"] = road_style
        style_config["tunnel_style"] = tunnel_style
        style_config["walk_style"] = walk_style
        
        save_style_config(style_config, STYLING_CONFIG_FPATH)
        st.session_state.map_html = None
        st.session_state.route_results = None
        
        st.success(f"Styles saved for {map_layer}")
        st.rerun()

if st.session_state.start_coords and st.session_state.end_coords and st.session_state.route_results is None:
    with st.spinner("Recalculating route..."):
        try:
            new_res = fetch_route_cached(
                st.session_state.start_coords, 
                st.session_state.end_coords,
                st.session_state.avoid_lights,
                st.session_state.avoid_turns
            )
            
            st.session_state.route_results = new_res
            map_visual = draw_route_map(
                new_res, st.session_state.start_coords, st.session_state.end_coords,
                map_layer, road_color, tunnel_color, walk_color, line_weight,
                road_style=LINE_STYLES[road_style],
                tunnel_style=LINE_STYLES[tunnel_style],
                walk_style=LINE_STYLES[walk_style],
            )
            st.session_state.map_html = map_visual._repr_html_()
            
        except Exception as e:
            st.sidebar.error(f"Error: {repr(e)}")

if st.sidebar.button("Calculate Fastest Route", key="calc_route_btn"):
    if st.session_state.start_coords and st.session_state.end_coords:
        st.session_state.route_results = None
        st.rerun()

if st.session_state.route_results:
    res = st.session_state.route_results
    # st.write("DEBUG API payload:", {
    #     "avoid_lights": st.session_state.avoid_lights,
    #     "avoid_turns": st.session_state.avoid_turns,
    #     "traffic_lights": res["traffic_lights"],
    #     "turns": res["turns"],
    #     "lights_delta": res["lights_delta"],
    #     "turns_delta": res["turns_delta"],
    #     "lights_time_delta": res["lights_time_delta"],
    #     "turns_time_delta": res["turns_time_delta"]
    # })
    start = st.session_state.start_coords
    end = st.session_state.end_coords
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Estimated Time", f"{res['time_min']:.1f} min")
    col2.metric("Total Distance", f"{res['dist_km']:.2f} km")
    col3.metric("Traffic Lights", f"{res.get('traffic_lights', 0)}")
    col4.metric("Turns", f"{res.get('turns', 0)}")
    if st.session_state.map_html:
        components.html(st.session_state.map_html, height=800, scrolling=False)

