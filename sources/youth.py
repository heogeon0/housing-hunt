"""청년안심주택 (서울시 역세권청년주택, soco.seoul.go.kr).

LH·SH 와 달리 매물·일정이 첨부에 숨어있지 않다. 목록이 JSON API 로 통째로 오고,
상세 페이지 본문에 단지개요·공급일정·주소·역이 텍스트로 박혀 있다. 셋 다 curl 로 된다.

  목록:  POST bbsListJson.json          -> JSON (nttSj, optn1 게시일, optn4 청약일, boardId)
  상세:  GET  view.do?boardId=N          -> 본문에 주택위치·공급호수·공급일정
  첨부:  GET  fileDown.do?atchFileId=..   -> PDF 모집공고문 (세부 임대료)

민간임대 일반공급은 소득·자산 기준이 없다(항상 통과, 단 자동차 소유·운행 금지).
그래서 자격 판정이 LH·SH 보다 단순하다.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .common import UA, curl, pdf_text, text

BASE = "https://soco.seoul.go.kr"
LIST_PAGE = f"{BASE}/youth/bbs/BMSR00015/list.do?menuNo=400008"
LIST_JSON = f"{BASE}/youth/pgm/home/yohome/bbsListJson.json"
VIEW = f"{BASE}/youth/bbs/BMSR00015/view.do?menuNo=400008&boardId="
FILE = f"{BASE}/coHouse/cmmn/file/fileDown.do?atchFileId="
BBS_ID = "BMSR00015"

# 청년 1인가구 대상. 신혼·고령·매입약정 등은 뺀다.
WANT = re.compile(r"청년|역세권|민간임대|공공임대")
SKIP = re.compile(r"신혼|고령|당첨자|결과|취소|정정\s*안내")


def announcements(pages: int = 2) -> list[dict]:
    """JSON API 로 목록을 가져온다."""
    out = []
    for page in range(1, pages + 1):
        body = {"bbsId": BBS_ID, "pageIndex": str(page), "searchAdresGu": "",
                "searchCondition": "", "searchKeyword": "", "optn2": "", "optn5": ""}
        raw = text(LIST_JSON, data=body, referer=LIST_PAGE)
        try:
            data = json.loads(raw)
        except Exception:
            break
        rows = data.get("resultList", [])
        for r in rows:
            out.append({
                "agency": "청년안심",
                "boardId": str(r.get("boardId", "")),
                "atchFileId": r.get("atchFileId", ""),
                "title": (r.get("nttSj") or "").strip(),
                "gu_cd": r.get("gubunCd", ""),
                "posted": (r.get("optn1") or "").replace("-", "."),
                "apply": (r.get("optn4") or "").replace("-", "."),
            })
        if not rows:
            break
    return out


def relevant(anns: list[dict]) -> list[dict]:
    return [a for a in anns if WANT.search(a["title"]) and not SKIP.search(a["title"])]


def _body(ann: dict) -> str:
    """상세 페이지 본문 텍스트. 캐시해 재요청을 줄인다."""
    if "_text" not in ann:
        html = text(VIEW + ann["boardId"], referer=LIST_PAGE)
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
        ann["_text"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return ann["_text"]


def _period(ann: dict) -> tuple[str, str]:
    """접수 시작·마감. 청년안심은 청약신청이 보통 하루라 시작=마감인 경우가 많다.
    상세 본문의 '청약신청 : 26. 07. 27.(월) 10:00 ~ 23:00' 을 읽는다."""
    t = _body(ann)
    m = re.search(r"청약신청[^:：]*[:：]\s*['‘’]?(\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\."
                  r"(?:[^~]*~\s*['‘’]?(\d{2})?\.?\s*(\d{1,2})?\.?\s*(\d{1,2})?\.?)?", t)
    if not m:
        # 목록 JSON 의 청약신청일로 대체
        return ann.get("apply", ""), ann.get("apply", "")
    y1, m1, d1, y2, m2, d2 = m.groups()
    start = f"20{y1}.{int(m1):02d}.{int(d1):02d}"
    if m2 and d2:
        end = f"20{y2 or y1}.{int(m2):02d}.{int(d2):02d}"
    else:
        end = start
    return start, end


def units(ann: dict) -> list[dict]:
    """상세 본문에서 단지 정보를 뽑는다. 신청 단위는 단지 하나(주택형별 세부는 PDF)."""
    t = _body(ann)

    loc = re.search(r"주택위치\s*[:：]\s*(.+?)(?:\s*■|\s*\[|공급호수)", t)
    addr = loc.group(1).strip() if loc else ""
    st = re.search(r"\(([^)]*역[^)]*)\)", addr)
    station = _station_name(st.group(1)) if st else ""
    addr_clean = re.sub(r"\s*\([^)]*\)\s*", "", addr).strip()

    gu = re.search(r"서울특별시\s+(\S+?구)", addr)
    supply = re.search(r"공급호수\s*[:：]\s*(.+?)(?:\s*■|\s*\[|사업주체)", t)
    houses = re.search(r"(\d+)\s*세대", supply.group(1)) if supply else None

    if not addr_clean:
        return []
    return [{
        "agency": "청년안심",
        "group": ann["title"],
        "addr": addr_clean,
        "station": station,
        "gu": gu.group(1) if gu else "",
        "count": int(houses.group(1)) if houses else 1,
        "supply": supply.group(1).strip() if supply else "",
        # 민간임대는 보증금·월세가 주택형마다 달라 PDF 에 있다. 요약엔 없음.
        "deposit": None,
        "rent": None,
    }]


def _station_name(raw: str) -> str:
    """'7호선, 경의중앙선, 경춘선 상봉역 5번 출구' -> '상봉'"""
    m = re.search(r"(\S+?)역", raw)
    return m.group(1) if m else ""


def groups(us: list[dict]) -> list[dict]:
    out = []
    for u in us:
        out.append({
            "agency": "청년안심",
            "unit": u["group"],
            "addr": u["addr"],
            "station": u.get("station", ""),
            "gu": u.get("gu", ""),
            "count": u["count"],
            "deposit": u["deposit"],
            "rent_min": None, "rent_max": None,
            "supply": u.get("supply", ""),
            "buildings": [{"addr": u["addr"], "count": u["count"], "share": 1.0}],
        })
    return out


def notice_text(ann: dict) -> str:
    """첨부 PDF (세부 임대료·자격). 없으면 상세 본문으로 대체."""
    if ann.get("atchFileId"):
        try:
            return pdf_text(curl(FILE + ann["atchFileId"] + "&fileSn=1", referer=VIEW))
        except Exception:
            pass
    return _body(ann)
