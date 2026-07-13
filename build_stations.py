#!/usr/bin/env python3
"""4호선 역 좌표를 한 번 받아 stations.json 으로 굳힌다.

매일 Overpass 를 때릴 이유가 없다. 역은 안 움직인다.
노선이 연장되거나 역이 추가되면 다시 돌리면 된다.
"""
import json
import pathlib
import subprocess
import time
import urllib.parse

# 당고개 -> 오이도. 순서가 곧 정거장 수 계산의 기준이다.
LINE4 = [
    "당고개", "상계", "노원", "창동", "쌍문", "수유", "미아", "미아사거리", "길음",
    "성신여대입구", "한성대입구", "혜화", "동대문", "동대문역사문화공원", "충무로",
    "명동", "회현", "서울역", "숙대입구", "삼각지", "신용산", "이촌", "동작",
    "총신대입구", "사당", "남태령", "선바위", "경마공원", "대공원", "과천",
    "정부과천청사", "인덕원", "평촌", "범계", "금정", "산본", "수리산", "대야미",
    "반월", "상록수", "한대앞", "중앙", "고잔", "초지", "안산", "신길온천",
    "정왕", "오이도",
]

# OSM 표기가 다른 역들
ALIAS = {
    "총신대입구": ["총신대입구(이수)", "이수"],
    "서울역": ["서울"],
    "당고개": ["불암산"],
    "동대문역사문화공원": ["동대문역사문화공원(DDP)"],
}

QUERY = """[out:json][timeout:60];
(
  node["railway"="station"](37.20,126.70,37.72,127.20);
  node["public_transport"="station"](37.20,126.70,37.72,127.20);
);
out body;"""


CACHE = pathlib.Path("_overpass.json")


def fetch() -> list[dict]:
    """Overpass 는 연속 요청에 rate limit 을 걸고 빈 응답을 준다. 캐시를 먼저 본다."""
    if CACHE.exists():
        print(f"  캐시 사용: {CACHE}")
        return json.loads(CACHE.read_text())["elements"]
    for attempt in range(4):
        raw = subprocess.run(
            ["curl", "-sS", "--max-time", "150", "-X", "POST",
             "https://overpass-api.de/api/interpreter",
             "--data-urlencode", "data=" + QUERY],
            capture_output=True).stdout
        if raw[:1] == b"{":
            CACHE.write_bytes(raw)
            return json.loads(raw)["elements"]
        print(f"  Overpass 빈 응답 (재시도 {attempt + 1}/4)")
        time.sleep(15)
    raise RuntimeError("Overpass 응답 실패")


def main():
    elements = fetch()

    coords = {}
    for e in elements:
        name = e.get("tags", {}).get("name", "").replace("역", "")
        if name and name not in coords:
            coords[name] = (round(e["lat"], 6), round(e["lon"], 6))

    # 4호선은 정거장 순서까지 안다 (직통이면 정거장 수로 시간을 낸다).
    stations, missing = [], []
    for i, name in enumerate(LINE4):
        hit = next((coords[a] for a in [name] + ALIAS.get(name, []) if a in coords), None)
        if hit:
            stations.append({"name": name, "idx": i, "lat": hit[0], "lon": hit[1],
                             "line4": True})
        else:
            missing.append(name)

    # 나머지 노선의 역도 넣는다. 4호선 역까지 걸어간다고 가정하면 서초·동작처럼
    # 2·3호선 타고 사당에서 갈아타는 동네가 부당하게 밀려난다.
    on4 = {s["name"] for s in stations}
    others = 0
    for name, (lat, lon) in coords.items():
        if name in on4:
            continue
        stations.append({"name": name, "idx": None, "lat": lat, "lon": lon,
                         "line4": False})
        others += 1

    out = {"workplace_station": "정부과천청사", "stations": stations}
    with open("stations.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"저장: 4호선 {len(LINE4) - len(missing)}/{len(LINE4)}개 + 기타 {others}개 "
          f"= {len(stations)}개 역")
    if missing:
        print("못 찾은 4호선 역:", ", ".join(missing))


if __name__ == "__main__":
    main()
