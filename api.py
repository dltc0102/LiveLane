import httpx, pyproj
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from urllib.parse import quote
from main import RoutingEngine

engine = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    print("Starting up: Loading Routing Engine...")
    client = httpx.AsyncClient(timeout=15.0)
    engine = RoutingEngine(client=client)
    await engine.apply_live_traffic()
    print("Engine loaded and traffic applied.")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

class CoordModel(BaseModel):
    lat: float
    lon: float

class RouteRequest(BaseModel):
    start: CoordModel
    end: CoordModel

class RouteResponse(BaseModel):
    time_min: float
    dist_km: float
    route: list[int]
    start_snapped: dict[str, float]
    end_snapped: dict[str, float]
    tunnel_bridge_edges: list[list[int]]

class AddressResult(BaseModel):
    display_text: str
    lat: float
    lon: float

@app.get("/geocode", response_model=list[AddressResult])
async def geocode_address(q: str):
    url = f"https://www.map.gov.hk/gs/api/v1.0.0/locationSearch?q={quote(q)}"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json()
            
            transformer = pyproj.Transformer.from_crs(2326, 4326, always_xy=True)
            results = []
            for item in data[:5]:
                if 'x' in item and 'y' in item:
                    lon, lat = transformer.transform(float(item['x']), float(item['y']))
                    results.append(AddressResult(
                        display_text=f"{item.get('address', q)} ({lat:.5f}, {lon:.5f})",
                        lat=lat, lon=lon
                    ))
            return results
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/route", response_model=RouteResponse)
async def calculate_route(req: RouteRequest):
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not loaded yet")
    
    start_dict = {'lat': req.start.lat, 'lon': req.start.lon}
    end_dict = {'lat': req.end.lat, 'lon': req.end.lon}
    
    try:
        route, start_node_coord, end_node_coord = engine.get_astar_route(start_dict, end_dict)
        
        if not route:
            raise HTTPException(status_code=404, detail="No driving route found between these points")
            
        total_time, total_dist = engine.get_route_stats(route)
        tunnel_bridge_edges = []
        
        for i in range(len(route) - 1):
            u, v = route[i], route[i+1]
            edge_data = engine.graph.get_edge_data(u, v)
            if edge_data:
                data = list(edge_data.values())[0]
                is_tunnel = data.get('tunnel') in [True, 'yes', 'building_passage']
                is_bridge = data.get('bridge') in [True, 'yes']
                if is_tunnel or is_bridge:
                    tunnel_bridge_edges.append([u, v])
        
        return RouteResponse(
            time_min=total_time / 60,
            dist_km=total_dist / 1000,
            route=route,
            start_snapped={"lat": start_node_coord[0], "lon": start_node_coord[1]},
            end_snapped={"lat": end_node_coord[0], "lon": end_node_coord[1]},
            tunnel_bridge_edges=tunnel_bridge_edges
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))