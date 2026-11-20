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

# pydantic models
class CoordModel(BaseModel):
    lat: float
    lon: float

class AddressResult(BaseModel):
    display_text: str
    lat: float
    lon: float

class RoutePreferences(BaseModel):
    avoid_lights: bool = False
    avoid_turns: bool = True

class RouteRequest(BaseModel):
    start: CoordModel
    end: CoordModel
    preferences: RoutePreferences = RoutePreferences()

class RouteResponse(BaseModel):
    time_min: float
    dist_km: float
    route: list[int]
    start_walk_path: list[list[float]] # list of lat lon sublists
    end_walk_path: list[list[float]]
    start_snapped: dict[str, float]
    end_snapped: dict[str, float]
    route_geometry: list[dict]
    traffic_lights: int
    turns: int
    lights_delta: int
    turns_delta: int
    lights_time_delta: float
    turns_time_delta: float


# endpoints
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
        # current route
        route, route_edges, start_node_coord, end_node_coord, start_walk_coords, end_walk_coords, current_lights, current_turns = engine.get_astar_route(
            start_dict, end_dict, preferences=req.preferences.model_dump()
        )
        if not route:
            raise HTTPException(status_code=404, detail="No driving route found between these points")
        total_time, total_dist = engine.get_route_stats(route)

        # lights
        opp_lights_prefs = {"avoid_lights": not req.preferences.avoid_lights, "avoid_turns": req.preferences.avoid_turns}
        opp_l_route, _, _, _, _, _, opp_l_lights, _ = engine.get_astar_route(start_dict, end_dict, preferences=opp_lights_prefs)
        opp_l_time, _ = engine.get_route_stats(opp_l_route) if opp_l_route else (total_time, 0)

        # turns
        opp_turns_prefs = {"avoid_lights": req.preferences.avoid_lights, "avoid_turns": not req.preferences.avoid_turns}
        opp_t_route, _, _, _, _, _, _, opp_t_turns = engine.get_astar_route(start_dict, end_dict, preferences=opp_turns_prefs)
        opp_t_time, _ = engine.get_route_stats(opp_t_route) if opp_t_route else (total_time, 0)

        # all deltas
        lights_delta = current_lights - opp_l_lights
        turns_delta = current_turns - opp_t_turns
        lights_time_delta = total_time - opp_l_time #s
        turns_time_delta = total_time - opp_t_time #s
        
        route_geometry = []
        for u, v, k in route_edges:
            data = engine.graph[u][v][k]
            tunnel_val = str(data.get('tunnel', '')).lower()
            bridge_val = str(data.get('bridge', '')).lower()
            is_tunnel = tunnel_val in ['yes', 'true', 'building_passage'] or bridge_val in ['yes', 'true']
            
            if 'geometry' in data:
                coords = [[lat, lon] for lon, lat in data['geometry'].coords]
            else:
                coords = [
                    [engine.graph.nodes[u]['y'], engine.graph.nodes[u]['x']],
                    [engine.graph.nodes[v]['y'], engine.graph.nodes[v]['x']]
                ]
            
            route_geometry.append({ "coords": coords, "is_tunnel": is_tunnel })

        return RouteResponse(
            time_min=total_time / 60, dist_km=total_dist / 1000, route=route,
            start_walk_path=start_walk_coords, end_walk_path=end_walk_coords,
            start_snapped={"lat": start_node_coord[0], "lon": start_node_coord[1]},
            end_snapped={"lat": end_node_coord[0], "lon": end_node_coord[1]},
            route_geometry=route_geometry,
            traffic_lights=current_lights, turns=current_turns,
            lights_delta=lights_delta, turns_delta=turns_delta,
            lights_time_delta=lights_time_delta, turns_time_delta=turns_time_delta
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))