"""GH 경기주택도시공사 (apply.gh.or.kr).

임대(sr7150)와 매입임대(sr7155) 두 목록을 본다. 둘 다 정적 HTML 이라 curl 로 된다.

  목록:  GET  sr71{50,55}/selectPbancRentHouseList.do  -> 행마다 data-pbancNo/bizTyNm
  상세:  POST selectPbancDetailView.do (pbancNo)        -> 본문에 단지·접수기간·주택형표
  첨부:  GET  selectFileDown.do?pbancNo=N&atchFileSn=..  -> 공고문 PDF (세부 임대료)

주의: 목록의 두 날짜는 '게시기간'이지 접수기간이 아니다. #801 은 목록에 07-10~07-20
으로 뜨지만 실제 접수처 운영기간은 07-20~07-22 다. LH 와 같은 함정. 마감 판정은
반드시 상세의 '접수처 운영기간'을 쓴다.

매물은 국민임대·통합공공임대는 본문 표에 있고(주택형·전용·세대수), 보증금·월세는
'공고문 확인'으로 비어 첨부 PDF 를 봐야 한다. 특화 매입임대는 본문이 얇고 첨부에 있다.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .common import curl, money, pdf_text, strip_tags, text

BASE = "https://apply.gh.or.kr"
LISTS = {
    "임대": f"{BASE}/sb/sr/sr7150/selectPbancRentHouseList.do",
    "매입": f"{BASE}/sb/sr/sr7155/selectPbancRentHouseList.do",
}
DETAIL = {
    "임대": f"{BASE}/sb/sr/sr7150/selectPbancDetailView.do",
    "매입": f"{BASE}/sb/sr/sr7155/selectPbancDetailView.do",
}
FILE = f"{BASE}/sr/sr7155/selectFileDown.do"

# 청년 1인가구가 신청 가능한 유형만.
WANT = re.compile(r"청년|행복주택|통합공공임대")
SKIP = re.compile(r"신혼|고령|다자녀|국민임대|장기전세|분양전환|일반매각|매각|"
                  r"자립준비|보호종료|정정공고|당첨자|결과")


def announcements() -> list[dict]:
    out = []
    for kind, url in LISTS.items():
        page = text(url, referer=f"{BASE}/")
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
            a = re.search(
                r'class="text_cut"[^>]*data-pbancNo="(\d+)"'
                r'[^>]*data-pbancKndCd="(\d+)"[^>]*data-bizTyNm="([^"]*)"[^>]*>(.*?)</a>',
                tr, re.S)
            if not a:
                continue
            title = strip_tags(a.group(4))
            dates = re.findall(r"(\d{4})-(\d{2})-(\d{2})", tr)
            out.append({
                "agency": "GH",
                "kind": kind,                 # 상세 URL 선택용
                "pbancNo": a.group(1),
                "pbancKndCd": a.group(2),
                "bizTyNm": a.group(3),
                "title": title,
                # 목록 날짜는 게시기간. 접수기간은 상세에서 다시 읽는다.
                "posted": ".".join(dates[0]) if dates else "",
            })
    return out


def relevant(anns: list[dict]) -> list[dict]:
    return [a for a in anns
            if WANT.search(a["title"]) and not SKIP.search(a["title"])]


def _body(ann: dict) -> str:
    if "_text" not in ann:
        html = text(DETAIL[ann["kind"]],
                    data={"pbancNo": ann["pbancNo"], "bizTyCd": ann["pbancKndCd"],
                          "molTyCd": ""},
                    referer=LISTS[ann["kind"]])
        ann["_html"] = html
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
        ann["_text"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return ann["_text"]


def _period(ann: dict) -> tuple[str, str]:
    """접수처 운영기간. 목록 날짜가 아니라 이걸 신뢰한다."""
    t = _body(ann)
    m = re.search(r"운영기간[^\d]*(\d{4})\.(\d{2})\.(\d{2})\s*~\s*(\d{4})\.(\d{2})\.(\d{2})", t)
    if not m:
        return "", ""
    g = m.groups()
    return f"{g[0]}.{g[1]}.{g[2]}", f"{g[3]}.{g[4]}.{g[5]}"


def units(ann: dict) -> list[dict]:
    """본문의 단지 정보 + 주택형 표. 보증금이 '공고문 확인'이면 첨부 PDF 로 보완한다."""
    _body(ann)
    html = ann.get("_html", "")
    t = ann["_text"]

    loc = re.search(r"소재지\s*(경기도\s+.+?)(?:전용면적|모집호수|난방|$)", t)
    addr = loc.group(1).strip() if loc else ""
    if not addr:
        # 특화 매입 등 본문이 얇은 유형: 소재지가 표에 없다. 첨부에 의존.
        return _units_from_pdf(ann)

    gu = re.search(r"경기도\s+(\S+?시|\S+?군)", addr)

    rows = _house_rows(html)
    need_pdf = any(u["deposit"] is None for u in rows)
    pdf_prices = _prices_from_pdf(ann) if need_pdf else {}

    out = []
    for u in rows:
        dep = u["deposit"]
        rent = u["rent"]
        if dep is None and u["htype"] in pdf_prices:
            dep, rent = pdf_prices[u["htype"]]
        out.append({
            "agency": "GH",
            "group": ann["title"],
            "addr": addr,
            "gu": gu.group(1) if gu else "",
            "htype": u["htype"],
            "area": u["area"],
            "count": u["count"],
            "deposit": dep, "rent": rent,
        })
    if not out:                     # 표는 없지만 소재지는 있는 경우
        out.append({"agency": "GH", "group": ann["title"], "addr": addr,
                    "gu": gu.group(1) if gu else "", "htype": "", "area": None,
                    "count": 1, "deposit": None, "rent": None})
    return out


def _house_rows(html: str) -> list[dict]:
    """본문 '주택형 안내' 표 -> 행.

    컬럼 구성이 공고마다 다르다. 어떤 표는 '예비입주자 세대수' 컬럼이 하나 더 껴서
    보증금 위치가 밀린다. 인덱스로 박으면 세대수를 보증금으로 잘못 읽는다.
    헤더 이름으로 컬럼을 찾는다.
    """
    i = html.find("주택형 안내")
    if i < 0:
        return []
    seg = html[i:i + 4500]
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)

    header = None
    for tr in trs:
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if cells and "주택형" in cells[0]:
            header = cells
            break
    if not header:
        return []

    def col(*keys):
        for j, h in enumerate(header):
            if any(k in h for k in keys):
                return j
        return None

    ci = {"htype": col("주택형"), "area": col("전용면적"),
          "count": col("금회공급", "공급 세대", "세대수"),
          "deposit": col("임대보증금", "보증금"), "rent": col("월임대료", "임대료")}

    out = []
    for tr in trs:
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if not cells or "주택형" in cells[0] or len(cells) < 3:
            continue

        def get(key):
            j = ci[key]
            return cells[j] if j is not None and j < len(cells) else ""

        htype = get("htype")
        area = _f(get("area"))
        # 주택형은 '51A' '34B1' 처럼 숫자+영문. 전용면적이 없으면 표 밖 텍스트다.
        if not re.match(r"\d{2,3}[A-Z]", htype) or area is None:
            continue
        out.append({
            "htype": htype,
            "area": area,
            "count": _i(get("count")),
            "deposit": money(get("deposit")),   # '공고문 확인' 은 money() 가 None
            "rent": money(get("rent")),
        })
    return out


def _prices_from_pdf(ann: dict) -> dict:
    """공고문 PDF 에서 주택형별 (보증금, 월세). 본문 표가 '공고문 확인' 일 때 보완."""
    txt = notice_text(ann)
    if not txt:
        return {}
    prices = {}
    # '33A ... 12,345,000 ... 234,560' 형태를 넓게 훑는다.
    for m in re.finditer(r"(\d{2,3}[A-Z]?)\D{0,40}?([\d,]{7,12})\D{0,20}?([\d,]{5,9})", txt):
        ht = m.group(1)
        if ht not in prices:
            prices[ht] = (money(m.group(2)), money(m.group(3)))
    return prices


def _units_from_pdf(ann: dict) -> list[dict]:
    """본문에 소재지가 없는 유형(특화 매입 등). 최소한 공고 존재만 알린다.
    세부 매물은 공고문 PDF 를 직접 보게 한다."""
    return []


def groups(us: list[dict]) -> list[dict]:
    """GH 는 단지 하나가 신청 단위. 주택형은 그 안의 평형."""
    by_addr = defaultdict(list)
    for u in us:
        by_addr[(u["group"], u["addr"])].append(u)
    out = []
    for (name, addr), members in by_addr.items():
        rents = [m["rent"] for m in members if m["rent"]]
        out.append({
            "agency": "GH",
            "unit": name,
            "addr": addr,
            "gu": members[0]["gu"],
            "count": sum(m["count"] or 0 for m in members) or len(members),
            "deposit": next((m["deposit"] for m in members if m["deposit"]), None),
            "rent_min": min(rents) if rents else None,
            "rent_max": max(rents) if rents else None,
            "buildings": [{"addr": addr, "count": 1, "share": 1.0}],
        })
    return sorted(out, key=lambda g: -g["count"])


def notice_text(ann: dict) -> str:
    """공고문 PDF. 첨부 목록에서 '공고문 ... .pdf' 를 고른다."""
    html = ann.get("_html") or ""
    if not html:
        _body(ann)
        html = ann.get("_html", "")
    m = re.search(r'(selectFileDown\.do\?[^"\']*?)"[^>]*>\s*([^<]*?공고문[^<]*?\.pdf)',
                  html, re.S)
    if not m:
        m = re.search(r'(selectFileDown\.do\?[^"\']*)"[^>]*>\s*([^<]*?\.pdf)', html, re.S)
    if not m:
        return ""
    url = m.group(1) if m.group(1).startswith("http") else f"{BASE}/sr/sr7155/{m.group(1)}"
    try:
        return pdf_text(curl(url, referer=DETAIL[ann["kind"]]))
    except Exception:
        return ""


def _f(s):
    m = re.search(r"[\d.]+", s or "")
    return float(m.group(0)) if m else None


def _i(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else None
