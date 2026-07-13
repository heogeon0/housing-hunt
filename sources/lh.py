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
            and WANT.search(a["title"]) and not SKIP.search(a["title"])]


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


def units(ann: dict) -> list[dict]:
    """첨부 엑셀 -> 매물. 컬럼 위치는 공고마다 흔들리므로 헤더가 아니라 값으로 잡는다."""
    fid, _ = _pick(attachments(ann), (".xlsx", ".xls"), r"목록|내역|현황")
    if not fid:
        return []
    rows = read_xlsx(curl(FILE + fid, referer=INFO))

    out = []
    for r in rows:
        if len(r) < 20 or not str(r[0]).strip().isdigit():
            continue
        addr = str(r[3]).strip()
        if not re.match(r"(서울|경기|인천)", addr):
            continue
        # 임대조건이 [1순위][2·3순위] 두 세트로 나오는 공고가 있다. 뒤 세트가 2·3순위.
        amounts = [i for i, v in enumerate(r) if (money(v) or 0) >= 100000]
        rent2 = money(r[22]) if len(r) > 22 else None
        dep2 = money(r[21]) if len(r) > 21 else None
        if rent2 is None or dep2 is None:
            dep2 = money(r[17]) if len(r) > 17 else None
            rent2 = money(r[18]) if len(r) > 18 else None
        out.append({
            "group": str(r[6]).strip(),          # 주택군 = 신청 단위
            "addr": addr,
            "ho": str(r[8]).strip(),
            "area": money(r[10]) or str(r[10]).strip(),
            "floor": str(r[14]).strip(),
            "elevator": str(r[15]).strip(),
            "htype": str(r[16]).strip(),
            "deposit": dep2, "rent": rent2,
            "_amounts": len(amounts),
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
