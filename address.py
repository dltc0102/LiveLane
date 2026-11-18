import re
import asyncio
import httpx
import pyproj
from dataclasses import dataclass
from difflib import SequenceMatcher


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


async def main() -> None:
    queries = [
        "2 lung pak street, world wide gardens, cypress court", 
        "the belchers, 89 pok fu lam road, tower 8"
    ]

    async with httpx.AsyncClient(headers=AddressQuery.DEFAULT_HEADERS, timeout=15.0) as client:
        aq = AddressQuery(client)
        for q in queries:
            print(f"\n=== {q!r} ===")
            for c in await aq.lookup(q):
                print(f"  {c.score:5.2f}  {c.display()}  -> ({c.lat:.5f}, {c.lon:.5f})")
            print(f"  best lat/lon: {await aq.latlon(q)}")

if __name__ == "__main__":
    asyncio.run(main())
