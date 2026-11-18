import httpx, asyncio, pyproj, re, os, folium
import streamlit as st
import osmnx as ox
import networkx as nx
from streamlit_folium import st_folium
from dataclasses import dataclass
from difflib import SequenceMatcher

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

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")

@st.cache_data(ttl=300, show_spinner=False)
def lookup_sync(query: str, top_n: int = 8) -> list[tuple[str, float, float]]:
    async def _run() -> list[AddressQuery.Candidate]:
        async with httpx.AsyncClient(headers=dict(AddressQuery.DEFAULT_HEADERS), timeout=15.0) as client:
            return await AddressQuery(client).lookup(query, top_n=top_n, min_score=0.0)
    return [(c.display(), c.lat, c.lon) for c in asyncio.run(_run())]


st.set_page_config(page_title="LiveLane", layout="wide")
st.title("LiveLane -- A* Route Engine")
st.markdown("Find the fastest route using live TD traffic data and A* routing.")

for key, default in {"start_coords": None, "end_coords": None, "route_results": None, "start_options": [], "end_options": [], "start_fetched": "", "end_fetched": ""}.items():
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

if not end_query:
    st.session_state.end_options = []
    st.session_state.end_fetched = ""
elif end_query != st.session_state.end_fetched:
    st.session_state.end_options = lookup_sync(end_query)
    st.session_state.end_fetched = end_query
    st.session_state.end_coords = None
    st.session_state.route_results = None
    st.session_state.pop("end_sel", None)

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

if st.sidebar.button("Calculate Fastest Route"):
    if st.session_state.start_coords and st.session_state.end_coords:
        with st.spinner("Calculating A* route..."):
            payload = {
                "start": {"lat": st.session_state.start_coords['lat'], "lon": st.session_state.start_coords['lon']},
                "end": {"lat": st.session_state.end_coords['lat'], "lon": st.session_state.end_coords['lon']},
            }
            try:
                transport = httpx.HTTPTransport(local_address="0.0.0.0")
                with httpx.Client(transport=transport, timeout=60.0) as client:
                    response = client.post(f"{API_URL}/route", json=payload)
                    response.raise_for_status()
                    st.session_state.route_results = response.json()
            except httpx.ConnectError as e:
                st.sidebar.error(f"ConnectError to {API_URL}: {repr(e)}")
            except httpx.TimeoutException as e:
                st.sidebar.error(f"Timeout after 60s: {repr(e)}")
            except httpx.HTTPStatusError as e:
                st.sidebar.error(f"Backend returned {e.response.status_code}: {e.response.text[:300]}")
            except Exception as e:
                st.sidebar.error(f"Unexpected error: {repr(e)}")

if st.session_state.route_results:
    res = st.session_state.route_results
    start = st.session_state.start_coords
    end = st.session_state.end_coords

    col1, col2 = st.columns(2)
    col1.metric("Estimated Time", f"{res['time_min']:.1f} min")
    col2.metric("Total Distance", f"{res['dist_km']:.2f} km")

    @st.cache_resource
    def get_map_graph():
        driving_data = ox.load_graphml("./data/hongkong_drive.graphml")
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

        try:
            driving_data = ox.truncate.largest_component(driving_data, strongly=False)
        except AttributeError:
            driving_data = ox.utils_graph.get_largest_component(driving_data, strongly=False)
            
        return driving_data

    graph = get_map_graph()

    bounds = [[min(start['lat'], end['lat']), min(start['lon'], end['lon'])], [max(start['lat'], end['lat']), max(start['lon'], end['lon'])]]
    center = calculate_center(start, end)
    m = folium.Map(location=center, zoom_start=12, tiles="Esri World Imagery")

    route_gdf = ox.routing.route_to_gdf(graph, res['route'])
    for _, row in route_gdf.iterrows():
        folium.PolyLine(
            [(lat, lon) for lon, lat in row.geometry.coords], 
            color="#00FFFF", weight=6, opacity=0.8
        ).add_to(m)

    if res.get('start_snapped'):
        folium.PolyLine(
            [(start['lat'], start['lon']), (res['start_snapped']['lat'], res['start_snapped']['lon'])],
            color="green", weight=4, dash_array='5, 10', 
            tooltip="Walk to start point"
        ).add_to(m)
        
    if res.get('end_snapped'):
        folium.PolyLine(
            [(res['end_snapped']['lat'], res['end_snapped']['lon']), (end['lat'], end['lon'])],
            color="green", weight=4, dash_array='5, 10',
            tooltip="Walk to destination"
        ).add_to(m)

    folium.Marker([start['lat'], start['lon']], icon=folium.Icon(color="green", icon="info-sign")).add_to(m)
    folium.Marker([end['lat'], end['lon']], icon=folium.Icon(color="red", icon="info-sign")).add_to(m)

    m.fit_bounds(bounds)
    st_folium(m, width=1000, height=600)