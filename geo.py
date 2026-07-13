"""주소 -> 좌표 -> 4호선 기준 출퇴근 시간 추정.

정확한 대중교통 경로 탐색은 하지 않는다. 우리가 필요한 건 순위를 매기는 것이지
분 단위 정답이 아니다. 그래서 '가장 가까운 4호선 역까지 걷는 시간 + 정거장 수'
로 근사한다. 4호선 축이 아닌 곳은 환승 페널티를 얹는다.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import time
import urllib.parse
from math import asin, cos, radians, sin, sqrt

HERE = pathlib.Path(__file__).parent
CACHE = HERE / ".geocache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

WALK_M_PER_MIN = 75          # 직선거리 기준. 실제 도보는 굽어지므로 아래에서 1.25배 보정
DETOUR = 1.25
MIN_PER_STOP = 2.2           # 4호선 표정속도
RAIL_M_PER_MIN = 530         # 환승 노선 구간의 실효 속도(정차 포함, 직선거리 기준)
TRANSFER_PENALTY = 10        # 사당 등에서 4호선으로 갈아타는 비용
BUS_PENALTY = 10             # 역이 멀면 버스를 한 번 더 탄다
FAR_FROM_STATION_M = 1200    # 이 거리를 넘으면 역세권이 아니다
BOARDING = 5                 # 대기·개찰


def _load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return {}


def _save_cache(c: dict) -> None:
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1))


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(h))


def _query(url_q: str) -> tuple[float, float] | None:
    url = f"{NOMINATIM}?{urllib.parse.urlencode({'format': 'json', 'limit': 1, 'countrycodes': 'kr', 'q': url_q})}"
    try:
        raw = subprocess.run(
            ["curl", "-sS", "-A", "housing-hunt/1.0", "--max-time", "20", url],
            capture_output=True, timeout=30).stdout
        hits = json.loads(raw)
    except Exception:
        hits = []
    time.sleep(1.1)  # Nominatim 정책상 초당 1회
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def geocode(addr: str) -> tuple[float, float] | None:
    """Nominatim 은 한국 주소에 약하다. 여러 표기로 차례로 시도하고 결과를 캐시한다.

    마지막 수단으로 동(洞)까지만 넣는다. 건물 좌표는 못 얻어도 동 중심이면
    출퇴근 시간 순위를 매기는 데는 충분하다.
    """
    cache = _load_cache()
    if addr in cache:
        v = cache[addr]
        return tuple(v) if v else None

    result = None
    for q in _candidates(addr):
        result = _query(q)
        if result:
            break

    cache[addr] = list(result) if result else None
    _save_cache(cache)
    return result


def _candidates(addr: str) -> list[str]:
    """'서울특별시 강동구 양재대로95길 36-9 (성내동 404-10)' 에서 시도 순서를 만든다."""
    import re
    inner = re.search(r"\((.*?)\)", addr)          # (성내동 404-10)
    bare = re.sub(r"\(.*?\)", " ", addr)
    parts = bare.split()
    gu = next((p for p in parts if p.endswith(("구", "시", "군"))), "")

    road = ""
    for i, p in enumerate(parts):
        if p.endswith(("로", "길")) or re.search(r"(로|길)\d*번?길?$", p):
            num = parts[i + 1].split("-")[0] if i + 1 < len(parts) else ""
            road = f"{p} {num}".strip()
            break

    out = []
    if road and gu:
        out.append(f"{road}, {gu}")
    if road:
        out.append(road)
    if inner:                                       # 지번: '성내동 404-10'
        dong = inner.group(1).split()[0]
        out.append(f"{inner.group(1)}, {gu}" if gu else inner.group(1))
        if gu:
            out.append(f"{dong}, {gu}")             # 최후: 동 중심 좌표
    out.append(bare.strip())
    seen, uniq = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


class Commute:
    """직장(정부과천청사, 4호선)까지의 소요시간을 근사한다.

    가장 가까운 역까지 걷고, 그 역이 4호선이면 정거장 수로 시간을 낸다.
    4호선이 아니면 그 역에서 4호선 환승역까지 타고 가는 시간을 직선거리로 근사한 뒤
    환승 비용을 얹는다. 서초·동작처럼 2·3호선을 타고 사당에서 갈아타는 동네를
    '4호선 역까지 걸어간다'고 잘못 계산하지 않기 위한 것이다.
    """

    def __init__(self, stations_path: pathlib.Path | None = None):
        data = json.loads((stations_path or HERE / "stations.json").read_text())
        self.stations = data["stations"]
        self.line4 = [s for s in self.stations if s.get("line4")]
        self.work = next(s for s in self.line4
                         if s["name"] == data["workplace_station"])

    def nearest(self, lat: float, lon: float, only4: bool = False) -> tuple[dict, float]:
        pool = self.line4 if only4 else self.stations
        best, best_d = None, float("inf")
        for s in pool:
            d = haversine(lat, lon, s["lat"], s["lon"])
            if d < best_d:
                best, best_d = s, d
        return best, best_d

    def estimate(self, addr: str) -> dict | None:
        """주소 -> {역, 도보분, 총분, 환승여부}. 좌표를 못 찾으면 None."""
        pos = geocode(addr)
        if not pos:
            return None
        station, dist = self.nearest(*pos)
        walk = dist * DETOUR / WALK_M_PER_MIN
        needs_bus = dist > FAR_FROM_STATION_M

        if station.get("line4"):
            stops = abs(station["idx"] - self.work["idx"])
            rail = stops * MIN_PER_STOP
            transfer = False
        else:
            # 이 역에서 가장 가까운 4호선 역으로 갈아탄다고 본다.
            hub, _ = self.nearest(station["lat"], station["lon"], only4=True)
            leg1 = haversine(station["lat"], station["lon"],
                             hub["lat"], hub["lon"]) / RAIL_M_PER_MIN
            leg2 = abs(hub["idx"] - self.work["idx"]) * MIN_PER_STOP
            rail = leg1 + leg2 + TRANSFER_PENALTY
            stops = abs(hub["idx"] - self.work["idx"])
            transfer = True

        total = walk + rail + BOARDING + (BUS_PENALTY if needs_bus else 0)
        return {
            "station": station["name"],
            "walk_min": round(walk),
            "walk_m": round(dist),
            "stops": stops,
            "total_min": round(total),
            "needs_bus": needs_bus,
            "transfer": transfer,
            "lat": pos[0], "lon": pos[1],
        }
