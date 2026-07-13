"""LH 청약플러스.

목록(GET) -> 상세(POST) -> 첨부 xlsx -> 매물.
매물 목록은 HTML 본문에 없고 첨부 엑셀에만 있다.

신청 단위는 '주택군'이고 호실은 무작위 배정이다. 주택군 하나가 여러 동네의
여러 건물을 통째로 묶기도 한다(예: 안산상록-2 = 본오동·사동·이동 7개 건물).
그래서 LH 추천은 건물이 아니라 주택군 단위로, 배정 확률 분포와 함께 해야 한다.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .common import curl, money, strip_tags, text, read_xlsx, pdf_text

BASE = "https://apply.lh.or.kr"
LIST = f"{BASE}/lhapply/apply/wt/wrtanc/selectWrtancList.do"
INFO = f"{BASE}/lhapply/apply/wt/wrtanc/selectWrtancInfo.do"  # View.do 가 아니다
FILE = f"{BASE}/lhapply/lhFile.do?fileid="
REF = f"{LIST}?mi=1026"

# 청년 1인가구가 신청할 수 있는 유형만
WANT = re.compile(r"청년|매입임대|전세임대|행복주택")
SKIP = re.compile(r"다자녀|고령자|신혼|신생아|장애|귀농|수급자|영구임대|국민임대|"
                  r"50년|분양전환|기숙사형")
# LH 는 전국 공고를 한 목록에 쏟는다. 출퇴근이 불가능한 지역은 아예 뺀다.
# 안 그러면 브리핑이 경남 사천·울산·세종 공고로 도배된다.
OUT_OF_REGION = re.compile(
    r"경남|경북|전남|전북|충남|충북|강원|제주|울산|부산|대구|광주|대전|세종|"
    r"사천|의령|거창|김제|정읍|남원|익산|군산|당진|예산|물금|철원|영월|제천|"
    r"옹진|백령|양산|창원|진주|포항|구미|천안|청주|춘천|강릉|원주|여주|연천|가평|양평")


def _rows(page: str) -> list[dict]:
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        ids = re.search(
            r'data-id1="([^"]+)"\s+data-id2="([^"]+)"\s+data-id3="([^"]+)"\s+data-id4="([^"]+)"',
            tr)
        if not ids:
            continue
        body = strip_tags(tr)
        span = re.search(r"<span>(.*?)</span>", tr, re.S)
        title = strip_tags(span.group(1)) if span else body
        title = re.sub(r"\s*(\d+일전|NEW)$", "", title).strip()
        dates = re.findall(r"(\d{4}\.\d{2}\.\d{2})", body)
        status = ("접수중" if "접수중" in body else
                  "접수마감" if "접수마감" in body else
                  "공고중" if "공고중" in body else "")
        out.append({
            "agency": "LH",
            "panId": ids.group(1), "ccrCnntSysDsCd": ids.group(2),
            "uppAisTpCd": ids.group(3), "aisTpCd": ids.group(4),
            "title": title, "status": status,
            "posted": dates[0] if dates else "",
            "deadline": dates[1] if len(dates) > 1 else "",
        })
    return out


def announcements(pages: int = 3) -> list[dict]:
    """게시일 역순이라 1페이지만 보면 놓친다. 여러 페이지를 훑는다."""
    first = text(f"{LIST}?mi=1026")
    tk = re.search(r'name="csrfToken"[^>]*value="([^"]+)"', first)
    token = tk.group(1) if tk else ""

    seen, found = set(), []
    for page in range(1, pages + 1):
        body = {"csrfToken": token, "mi": "1026", "currPage": str(page),
                "listCo": "100", "srchUppAisTpCd": "061339", "uppAisTpCd": "06",
                "schTy": "0", "srchY": "Y"}
        got = _rows(text(LIST, data=body, referer=REF))
        fresh = [r for r in got if r["panId"] not in seen]
        seen.update(r["panId"] for r in fresh)
        found += fresh
        if len(got) < 100:
            break
    return found


def relevant(anns: list[dict]) -> list[dict]:
    return [a for a in anns
            if a["status"] in ("접수중", "공고중")
            and WANT.search(a["title"])
            and not SKIP.search(a["title"])
            and not OUT_OF_REGION.search(a["title"])]


def attachments(ann: dict) -> list[tuple[str, str]]:
    """상세(POST) -> [(fileid, 파일명)]"""
    body = {"mi": "1026", "panId": ann["panId"],
            "ccrCnntSysDsCd": ann["ccrCnntSysDsCd"],
            "uppAisTpCd": ann["uppAisTpCd"], "aisTpCd": ann["aisTpCd"]}
    page = text(INFO, data=body, referer=REF)
    if "오류알림" in page:
        return []
    pairs = re.findall(
        r"fileDownLoad\('(\d+)'\)[^>]*>\s*(?:<[^>]+>\s*)*"
        r"([^<]{3,120}?\.(?:hwp|hwpx|pdf|xlsx|xls|zip))", page, re.I | re.S)
    return [(fid, name.strip()) for fid, name in pairs]


def _pick(atts, exts, must=None):
    for fid, name in atts:
        if name.lower().endswith(exts) and (not must or re.search(must, name)):
            return fid, name
    return None, None


class LayoutError(Exception):
    """엑셀 배치를 못 읽었다. 조용히 빈 리스트를 내지 않는다."""


def _columns(rows: list[list[str]]) -> dict:
    """헤더를 읽어 컬럼을 이름으로 찾는다.

    컬럼 위치는 공고마다 다르다. 실제로 [울산권] 매입임대는 컬럼이 14개뿐이고
    주소가 [2], 보증금이 [10]이다. 경기남부 청년매입은 29개에 주소가 [3]이다.
    인덱스를 박아두면 배치가 다른 공고에서 엉뚱한 칸을 읽는다.

    임대조건은 두 세트로 나온다. 병합 헤더 윗줄에 '청년 수급자'(1순위)와
    '청년 일반'(2·3순위)이 있고, 아랫줄에 각각 임대보증금/월임대료가 붙는다.
    일반 청년 신청자에게 필요한 건 '일반' 쪽이다. 수급자 조건을 쓰면 월세를
    20%쯤 과소평가한다.
    """
    field_row = group_row = None
    for i, r in enumerate(rows):
        labels = [str(c).strip() for c in r]
        if "주소" in labels and any("임대보증금" in x for x in labels):
            field_row, group_row = r, rows[i - 1] if i else []
            break
    if not field_row:
        raise LayoutError("헤더(주소/임대보증금) 행을 못 찾았다")

    def find(*names, start=0):
        for j, c in enumerate(field_row):
            if j < start:
                continue
            flat = re.sub(r"\s+", "", str(c))
            if any(re.sub(r"\s+", "", n) == flat for n in names):
                return j
        return None

    # '청년 일반'(2·3순위) 그룹이 시작하는 컬럼
    general = 0
    for j, c in enumerate(group_row):
        if "일반" in str(c):
            general = j
            break

    cols = {
        "addr": find("주소"),
        "group": find("주택군"),
        "ho": find("호"),
        "area": find("전용면적"),
        "floor": find("층수"),
        "elevator": find("승강기유무"),
        "htype": find("주택유형"),
        "deposit": find("임대보증금", start=general),
        "rent": find("월임대료", start=general),
    }
    if cols["addr"] is None or cols["deposit"] is None or cols["rent"] is None:
        raise LayoutError(f"필수 컬럼을 못 찾았다: {cols}")
    return cols


def units(ann: dict) -> list[dict]:
    """첨부 엑셀 -> 매물."""
    fid, _ = _pick(attachments(ann), (".xlsx", ".xls"), r"목록|내역|현황")
    if not fid:
        return []
    rows = read_xlsx(curl(FILE + fid, referer=INFO))
    cols = _columns(rows)

    def cell(r, key):
        j = cols[key]
        return str(r[j]).strip() if j is not None and j < len(r) else ""

    out = []
    for r in rows:
        if not str(r[0]).strip().isdigit():
            continue
        addr = cell(r, "addr")
        if not re.match(r"(서울|경기|인천)", addr):
            continue
        out.append({
            "group": cell(r, "group") or "전체",   # 주택군 = 신청 단위
            "addr": addr,
            "ho": cell(r, "ho"),
            "area": cell(r, "area"),
            "floor": cell(r, "floor"),
            "elevator": cell(r, "elevator"),
            "htype": cell(r, "htype"),
            # 2·3순위 값이 없으면 1순위(수급자) 조건으로 대체하지 않는다.
            # 틀린 월세를 내느니 '확인 필요'가 낫다.
            "deposit": money(cell(r, "deposit")),
            "rent": money(cell(r, "rent")),
        })
    return out


def groups(us: list[dict]) -> list[dict]:
    """주택군(신청 단위)으로 묶는다. 호실은 무작위 배정이므로 확률 분포가 핵심이다."""
    by_group = defaultdict(list)
    for u in us:
        by_group[u["group"]].append(u)

    out = []
    for name, members in by_group.items():
        by_addr = defaultdict(list)
        for m in members:
            by_addr[m["addr"]].append(m)
        rents = [m["rent"] for m in members if m["rent"]]
        out.append({
            "agency": "LH",
            "unit": name,                 # 신청 단위 이름
            "count": len(members),
            "buildings": [                # 어디에 걸릴지 모른다 -> 확률 분포
                {"addr": a, "count": len(v), "share": len(v) / len(members)}
                for a, v in sorted(by_addr.items(), key=lambda kv: -len(kv[1]))
            ],
            "rent_min": min(rents) if rents else None,
            "rent_max": max(rents) if rents else None,
            "deposit": members[0]["deposit"],
            "htype": members[0]["htype"],
        })
    return sorted(out, key=lambda g: -g["count"])


def notice_text(ann: dict) -> str:
    """공고문 PDF 전문. 소득·자산 기준은 여기서 읽는다 (하드코딩 금지)."""
    fid, _ = _pick(attachments(ann), (".pdf",))
    return pdf_text(curl(FILE + fid, referer=INFO)) if fid else ""
