"""주소 -> 좌표 -> 직장까지의 출퇴근 시간.

전에는 "가장 가까운 4호선 역까지 직선거리로 간다"고 근사했다. 그랬더니 선로가
없는 구간을 지어냈다. 오리역(수인분당선)에서 인덕원(4호선)까지 13.5km를 직선으로
"타고" 용인수지를 52분으로 매겼는데, 두 역을 잇는 노선은 존재하지 않고 실제로는
65~70분이 걸린다. 버스터미널 노드에 스냅되는 경우까지 있었다.

지금은 실제 노선 그래프에서 최단경로를 찾는다. 환승은 노선이 바뀔 때만 비용을
문다. 정확한 시각표 기반 경로탐색은 아니지만, 없는 노선을 지어내지는 않는다.
필요한 건 순위지 분 단위 정답이 아니다.
"""
from __future__ import annotations

import heapq
import json
import pathlib
import re
import subprocess
import time
import urllib.parse
from math import asin, cos, radians, sin, sqrt

HERE = pathlib.Path(__file__).parent
CACHE = HERE / ".geocache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

WALK_M_PER_MIN = 75      # 직선거리 기준. 실제 도보는 굽어지므로 DETOUR 로 보정
DETOUR = 1.25
# 정거장당 고정 2.2분으로 잡으면 역간 거리가 먼 구간(안산선 등)을 과소평가한다.
# 실제 역간 거리로 계산하고, 정차 시간을 더한다.
RAIL_M_PER_MIN = 620     # 주행 속도(약 37km/h)
DWELL_MIN = 0.6          # 역당 정차·감가속
MIN_HOP_MIN = 1.5
TRANSFER_MIN = 8         # 환승 1회
TRANSFER_WALK_M = 300    # 이 거리 안의 다른 이름 역은 같은 환승역으로 본다
BOARDING = 5             # 대기·개찰
BUS_PENALTY = 10         # 역이 멀면 버스를 한 번 더 탄다
FAR_FROM_STATION_M = 1200
CANDIDATES = 5           # 가장 가까운 역이 반대 방향일 수 있다. 여러 역을 후보로 둔다


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(h))


# --- 지오코딩 ---------------------------------------------------------------

def _load_cache() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def _query(q: str) -> tuple[float, float] | None:
    url = f"{NOMINATIM}?" + urllib.parse.urlencode(
        {"format": "json", "limit": 1, "countrycodes": "kr", "q": q})
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
    """Nominatim 은 한국 주소에 약하다. 도로명 -> 지번 -> 동 순으로 시도하고 캐시한다."""
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
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    return result


def _candidates(addr: str) -> list[str]:
    inner = re.search(r"\((.*?)\)", addr)
    bare = re.sub(r"\(.*?\)", " ", addr)
    parts = bare.split()
    gu = next((p for p in parts if p.endswith(("구", "시", "군"))), "")

    road = ""
    for i, p in enumerate(parts):
        if p.endswith(("로", "길")) or re.search(r"(로|길)\d*번?길?$", p):
            num = parts[i + 1].split("-")[0] if i + 1 < len(parts) else ""
            road = f"{p} {num}".strip()
            break

    # 시/구를 반드시 붙인다. 도로명만으로 재시도하면 엉뚱한 도시로 간다.
    # '언남로 18'만 던지면 서초구가 아니라 용인 언남동이 잡혔다.
    city = parts[0] if parts and parts[0].endswith(("시", "도")) else ""
    where = ", ".join(x for x in (gu, city) if x)

    out = []
    if road and where:
        out.append(f"{road}, {where}")
    if inner and where:
        out.append(f"{inner.group(1)}, {where}")
        out.append(f"{inner.group(1).split()[0]}, {where}")  # 최후: 동 중심
    out.append(bare.strip())

    seen, uniq = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


# --- 출퇴근 -----------------------------------------------------------------

class Commute:
    def __init__(self, path: pathlib.Path | None = None):
        data = json.loads((path or HERE / "stations.json").read_text())
        self.stations = {s["name"]: s for s in data["stations"]}
        self.work = data["workplace"]
        if self.work not in self.stations:
            raise RuntimeError(f"직장역 '{self.work}' 이 그래프에 없다")

        # (역, 노선) -> [(이웃역, 노선, 소요분)]
        self.adj: dict[tuple, list[tuple]] = {}
        for a, b, line in data["edges"]:
            sa, sb = self.stations.get(a), self.stations.get(b)
            if not sa or not sb:
                continue
            d = haversine(sa["lat"], sa["lon"], sb["lat"], sb["lon"])
            cost = max(MIN_HOP_MIN, d / RAIL_M_PER_MIN + DWELL_MIN)
            self.adj.setdefault((a, line), []).append((b, line, cost))

        # 같은 환승역인데 노선마다 이름이 다른 경우가 있다. 4호선은 '총신대입구',
        # 7호선은 '이수'로 등록돼 있어서 이름만으로 이으면 환승이 끊긴다.
        # 그 탓에 상도동(7호선)의 철도 시간이 55분으로 나왔다. 실제는 30분대다.
        # 좌표가 가까운 역끼리도 환승으로 잇는다.
        self.nearby: dict[str, list[str]] = {}
        names = list(self.stations)
        for i, a in enumerate(names):
            sa = self.stations[a]
            for b in names[i + 1:]:
                sb = self.stations[b]
                if abs(sa["lat"] - sb["lat"]) > 0.004:
                    continue
                if haversine(sa["lat"], sa["lon"], sb["lat"], sb["lon"]) <= TRANSFER_WALK_M:
                    self.nearby.setdefault(a, []).append(b)
                    self.nearby.setdefault(b, []).append(a)

        self.rail = self._times_to_work()

    def _times_to_work(self) -> dict[str, float]:
        """직장역에서 모든 역까지의 최소 소요시간. 한 번만 계산한다.

        상태를 (역, 타고 있는 노선)으로 둬야 환승 비용을 제대로 문다.
        같은 역에서 노선을 갈아타면 TRANSFER_MIN 을 더한다.
        """
        dist: dict[tuple, float] = {}
        pq = [(0.0, self.work, line) for line in self.stations[self.work]["lines"]]
        heapq.heapify(pq)
        for line in self.stations[self.work]["lines"]:
            dist[(self.work, line)] = 0.0

        while pq:
            d, station, line = heapq.heappop(pq)
            if d > dist.get((station, line), float("inf")):
                continue
            # 같은 역에서 다른 노선으로 환승
            for other in self.stations[station]["lines"]:
                if other == line:
                    continue
                nd = d + TRANSFER_MIN
                if nd < dist.get((station, other), float("inf")):
                    dist[(station, other)] = nd
                    heapq.heappush(pq, (nd, station, other))
            # 이름은 다르지만 실제로는 같은 환승역(총신대입구 <-> 이수)
            for twin in self.nearby.get(station, []):
                for other in self.stations[twin]["lines"]:
                    nd = d + TRANSFER_MIN
                    if nd < dist.get((twin, other), float("inf")):
                        dist[(twin, other)] = nd
                        heapq.heappush(pq, (nd, twin, other))
            # 같은 노선으로 한 정거장
            for nxt, ln, cost in self.adj.get((station, line), []):
                nd = d + cost
                if nd < dist.get((nxt, ln), float("inf")):
                    dist[(nxt, ln)] = nd
                    heapq.heappush(pq, (nd, nxt, ln))

        best: dict[str, float] = {}
        for (station, _), d in dist.items():
            if d < best.get(station, float("inf")):
                best[station] = d
        return best

    def estimate(self, addr: str) -> dict | None:
        """좌표를 못 찾으면 None. 가장 가까운 역이 반대 방향일 수 있으므로
        후보 여러 개를 놓고 '도보 + 철도'의 합이 가장 작은 것을 고른다."""
        pos = geocode(addr)
        if not pos:
            return None

        near = sorted(
            ((haversine(*pos, s["lat"], s["lon"]), s) for s in self.stations.values()),
            key=lambda x: x[0])[:CANDIDATES]

        best = None
        for dist_m, s in near:
            rail = self.rail.get(s["name"])
            if rail is None:      # 직장과 연결되지 않은 노선
                continue
            walk = dist_m * DETOUR / WALK_M_PER_MIN
            far = dist_m > FAR_FROM_STATION_M
            total = walk + rail + BOARDING + (BUS_PENALTY if far else 0)
            cand = {
                "station": s["name"],
                "lines": s["lines"],
                "walk_min": round(walk),
                "walk_m": round(dist_m),
                "rail_min": round(rail),
                "total_min": round(total),
                "needs_bus": far,
                "lat": pos[0], "lon": pos[1],
            }
            if best is None or cand["total_min"] < best["total_min"]:
                best = cand
        return best
