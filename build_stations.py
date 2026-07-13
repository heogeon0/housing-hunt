#!/usr/bin/env python3
"""수도권 전철 노선 그래프를 만들어 stations.json 으로 굳힌다.

역 좌표만 모아두고 "가장 가까운 4호선 역까지 직선거리로 간다"고 근사했더니
존재하지 않는 환승을 지어냈다. 오리역(수인분당선)에서 인덕원(4호선)까지
13.5km를 직선으로 "탄다"고 계산해서 용인수지를 52분으로 매겼다. 실제로는
그 두 역을 잇는 노선이 없고 65~70분이 걸린다. 버스터미널 노드에 스냅되는
경우까지 있었다.

그래서 OSM 노선 관계(route relation)에서 역의 순서를 읽어 실제 인접 그래프를
만든다. 환승은 같은 이름의 역끼리 이어주고, 노선을 갈아탈 때만 비용을 물린다.

역은 안 움직인다. 한 번 만들어두고 노선이 연장될 때만 다시 돌린다.
"""
import json
import pathlib
import subprocess
import time

CACHE = pathlib.Path("_subway.json")

# 수도권 전철 전체.
# subway 만 받으면 수인·분당선과 경의·중앙선이 빠진다. 둘은 route=train 인데
# 이름이 '수인·분당선: 고색 → 왕십리' 처럼 '수도권'으로 시작하지도 않는다.
# 이게 빠지면 수원·용인 매물의 경로가 통째로 틀린다.
QUERY = """[out:json][timeout:240];
(
  rel["route"="subway"](37.10,126.55,37.80,127.35);
  rel["route"="light_rail"](37.10,126.55,37.80,127.35);
  rel["route"="train"]["name"~"수인|분당|경의|중앙|경춘|경강|서해|공항|수도권"](37.10,126.55,37.80,127.35);
);
out body;
node(r);
out body;"""

WORKPLACE = "정부과천청사"


def fetch() -> list[dict]:
    if CACHE.exists():
        print(f"  캐시 사용: {CACHE}")
        return json.loads(CACHE.read_text())["elements"]
    for attempt in range(4):
        raw = subprocess.run(
            ["curl", "-sS", "--max-time", "300", "-X", "POST",
             "https://overpass-api.de/api/interpreter",
             "--data-urlencode", "data=" + QUERY],
            capture_output=True).stdout
        if raw[:1] == b"{":
            CACHE.write_bytes(raw)
            return json.loads(raw)["elements"]
        print(f"  Overpass 빈 응답 (재시도 {attempt + 1}/4)")
        time.sleep(20)
    raise RuntimeError("Overpass 응답 실패")


def line_name(tags: dict) -> str:
    """'수도권 전철 4호선: 불암산 → 오이도' -> '수도권 전철 4호선' (방향별 중복 제거)"""
    name = tags.get("name", "")
    return name.split(":")[0].strip() or tags.get("ref", "?")


def main():
    elements = fetch()
    nodes = {e["id"]: e for e in elements if e["type"] == "node"}
    rels = [e for e in elements if e["type"] == "relation"]

    stations: dict[str, dict] = {}   # 역이름 -> {lat, lon, lines}
    edges: set[tuple] = set()        # (역A, 역B, 노선)

    for rel in rels:
        line = line_name(rel.get("tags", {}))
        seq = []
        for m in rel.get("members", []):
            if m["type"] != "node" or m.get("role") not in ("stop", "stop_exit_only",
                                                            "stop_entry_only", ""):
                continue
            n = nodes.get(m["ref"])
            if not n:
                continue
            name = n.get("tags", {}).get("name", "").strip()
            if not name:
                continue
            name = name.replace("역", "") if name.endswith("역") else name
            if seq and seq[-1][0] == name:
                continue
            seq.append((name, n["lat"], n["lon"]))

        for name, lat, lon in seq:
            s = stations.setdefault(name, {"lat": lat, "lon": lon, "lines": []})
            if line not in s["lines"]:
                s["lines"].append(line)
        for (a, *_), (b, *_) in zip(seq, seq[1:]):
            if a != b:
                edges.add((a, b, line))
                edges.add((b, a, line))

    if WORKPLACE not in stations:
        raise RuntimeError(f"직장역 '{WORKPLACE}' 을 못 찾았다")

    out = {
        "workplace": WORKPLACE,
        "stations": [{"name": k, **v} for k, v in sorted(stations.items())],
        "edges": sorted(edges),
    }
    pathlib.Path("stations.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1))

    lines = {ln for s in stations.values() for ln in s["lines"]}
    print(f"저장: 역 {len(stations)}개, 구간 {len(edges) // 2}개, 노선 {len(lines)}개")
    print(f"직장역: {WORKPLACE} — 노선 {stations[WORKPLACE]['lines']}")
    for probe in ("사당", "오리", "한대앞", "상록수", "수원"):
        if probe in stations:
            print(f"  {probe}: {stations[probe]['lines']}")


if __name__ == "__main__":
    main()
