"""SH 서울주택도시공사.

목록(POST) -> 상세(POST) -> 첨부 실경로(302 Location) -> PDF -> 매물.
매물 목록은 첨부 PDF 에만 있다. xlsx 가 아니다.

신청 단위는 '주소지(주택형)' 이다. 즉 건물과 평형은 내가 고르고, 층·호수만 추첨이다.
LH 의 주택군(여러 건물을 묶음)과 달리 위치를 확정해서 지를 수 있다.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .common import curl, pdf_text, text

BASE = "https://www.i-sh.co.kr"
BRD = f"{BASE}/app/lay2/program/S48T1581C563/www/brd/m_247"
LIST = f"{BRD}/list.do"
VIEW = f"{BRD}/view.do"
CONV = f"{BASE}/app/com/util/htmlConverter.do"

WANT = re.compile(r"청년|행복주택|매입임대|장기전세")
# 모집공고가 아닌 것들. 이걸 안 걸면 '당첨자 발표'를 공고로 착각한다.
# 기숙사형은 성별 제한·공동생활이라 대상에서 뺀다(주택목록 표 형식도 달라 파싱이 깨진다).
SKIP = re.compile(r"신혼|고령|다자녀|주거약자|국민임대|재개발|기숙사형|"
                  r"당첨자|발표|경쟁률|계약|취소|정정 안내|알림|안내문|결과")


def announcements(keyword: str = "", pages: int = 5) -> list[dict]:
    """목록도 게시일 역순이라 1페이지만 보면 놓친다. 접수 기간이 긴 공고가 밀려난다."""
    seen, out = set(), []
    for page in range(1, pages + 1):
        body = text(LIST, data={"multi_itm_seq": "2", "srchTp": "0",
                                "srchWord": keyword, "page": str(page)})
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        found = 0
        for tr in rows:
            seq = re.search(r"getDetailView\('(\d+)'\)", tr)
            if not seq or seq.group(1) in seen:
                continue
            seen.add(seq.group(1))
            found += 1
            flat = " ".join(re.sub(r"<[^>]+>", " ", tr).split())
            dates = re.findall(r"(\d{4}[-.]\d{2}[-.]\d{2})", flat)
            title = re.search(r"getDetailView\('\d+'\)[^>]*>\s*([^<]{4,120})", tr)
            out.append({
                "agency": "SH",
                "seq": seq.group(1),
                "title": (title.group(1).strip() if title else flat[:80]),
                "posted": dates[0] if dates else "",
            })
        if not found:
            break
    return out


def relevant(anns: list[dict]) -> list[dict]:
    return [a for a in anns if WANT.search(a["title"]) and not SKIP.search(a["title"])]


def attachments(ann: dict) -> list[dict]:
    """상세 본문에 첨부 목록이 JSON 으로 인라인돼 있다."""
    page = text(VIEW, data={"multi_itm_seq": "2", "seq": ann["seq"], "page": "1"})
    out = []
    for m in re.finditer(
            r'\{[^{}]*"brdId"\s*:\s*"([^"]+)"[^{}]*"fileSeq"\s*:\s*"?(\d+)"?'
            r'[^{}]*"oriFileNm"\s*:\s*"([^"]+)"[^{}]*\}', page):
        out.append({"brd_id": m.group(1), "file_seq": m.group(2), "name": m.group(3)})
    if not out:  # 키 순서가 다른 경우를 위한 보정
        for m in re.finditer(r'"fileSeq"\s*:\s*"?(\d+)"?.*?"oriFileNm"\s*:\s*"([^"]+)"', page):
            out.append({"brd_id": "GS0401", "file_seq": m.group(1), "name": m.group(2)})
    return out


def download(ann: dict, att: dict) -> bytes:
    """다운로드 버튼은 JS 다운로더(Innorix)에 묶여 있어 막힌다.
    대신 '미리보기'(htmlConverter.do) 의 302 Location 이 실제 저장 경로를 흘린다.
    날짜 경로와 해시 파일명이 공고마다 바뀌므로 매번 Location 에서 뽑아야 한다."""
    url = (f"{CONV}?brd_id={att['brd_id']}&seq={ann['seq']}"
           f"&data_tp=A&file_seq={att['file_seq']}")
    head = curl(url, headers=True, referer=VIEW).decode("utf-8", "replace")
    loc = re.search(r"^[Ll]ocation:\s*(\S+)", head, re.M)
    if not loc:
        raise RuntimeError(f"302 Location 없음 (UA 차단?): {att['name']}")
    q = loc.group(1)
    fn = re.search(r"[?&]fn=([^&\s]+)", q)
    rs = re.search(r"[?&]rs=([^&\s]+)", q)
    if not (fn and rs):
        raise RuntimeError(f"fn/rs 파싱 실패: {q}")
    ext = att["name"].rsplit(".", 1)[-1]
    path = re.sub(r"html/?$", "", rs.group(1))  # .../2026/06/html/ -> .../2026/06/
    return curl(f"{BASE}{path}{fn.group(1)}.{ext}", referer=VIEW)


def _find(atts, pattern):
    for a in atts:
        if re.search(pattern, a["name"]):
            return a
    return None


MONEY_TAIL = r"([\d.]+)\s+((?:(?:[\d,]+|-)\s+){7}(?:[\d,]+|-))$"


def units(ann: dict) -> list[dict]:
    """주택목록 PDF -> 매물.

    PDF 텍스트 추출이 컬럼 경계 공백을 자주 흘린다(`동대문구0201`). 그래서 왼쪽부터
    자르지 않고, 오른쪽 금액 8개(보증금/임대료 4쌍)를 앵커로 잡고 역으로 푼다.
    금액 4쌍 = [1순위][2·3순위] x [청년][대학생·취준생].
    """
    atts = attachments(ann)
    target = _find(atts, r"주택목록")
    if not target:
        return []
    body = pdf_text(download(ann, target))

    out, bad = [], 0
    for line in body.split("\n"):
        line = line.strip()
        if not re.match(r"^\d+\s+(신규공급|재공급)\s", line):
            continue
        tail = re.search(MONEY_TAIL, line)
        if not tail:
            bad += 1
            continue
        amounts = [None if x == "-" else int(x.replace(",", ""))
                   for x in tail.group(2).split()]
        head = line[:tail.start()].strip()
        hm = re.match(r"^(\d+)\s+(신규공급|재공급)\s+(\S+?구)\s*(\d{3,4})\s*(.+?)\s+"
                      r"(서울특별시\s.+)$", head)
        if not hm:
            bad += 1
            continue
        am = re.match(r"^(.+?\))\s+(\S+)\s+(.+?)\s*(남성|여성|-)$", hm.group(6))
        if not am:
            bad += 1
            continue
        out.append({
            "agency": "SH",
            "gu": hm.group(3),
            "name": hm.group(5),
            "addr": am.group(1),
            "htype": am.group(2),            # 주택형 (25B 등)
            "layout": am.group(3),
            "gender": am.group(4),
            "ho": hm.group(4),
            "area": float(tail.group(1)),
            "supply": hm.group(2),
            "deposit": amounts[4],           # 2·3순위 청년
            "rent": amounts[5],
        })
    if bad:
        print(f"  [경고] SH 주택목록 {bad}행 파싱 실패", flush=True)
    return out


def groups(us: list[dict]) -> list[dict]:
    """신청 단위 = (소재지, 주택형). 건물·평형은 고르고 층·호수만 추첨이다."""
    by_unit = defaultdict(list)
    for u in us:
        by_unit[(u["addr"], u["htype"])].append(u)

    out = []
    for (addr, htype), members in by_unit.items():
        m0 = members[0]
        rents = [m["rent"] for m in members if m["rent"]]
        out.append({
            "agency": "SH",
            "unit": f"{m0['name']} {htype}",
            "addr": addr,
            "gu": m0["gu"],
            "count": len(members),
            "area": m0["area"],
            "layout": m0["layout"],
            "gender": m0["gender"],
            "deposit": m0["deposit"],
            "rent_min": min(rents) if rents else None,
            "rent_max": max(rents) if rents else None,
            "buildings": [{"addr": addr, "count": len(members), "share": 1.0}],
        })
    return sorted(out, key=lambda g: -g["count"])


def notice_text(ann: dict) -> str:
    atts = attachments(ann)
    target = _find(atts, r"공고문")
    return pdf_text(download(ann, target)) if target else ""
