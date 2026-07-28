#!/usr/bin/env python3
"""청약·임대 공고를 긁어 출퇴근 기준으로 줄 세우고 브리핑을 만든다.

원칙: 이 스크립트에는 소득 기준·매물·마감일을 하드코딩하지 않는다.
전부 공고문 원문에서 읽는다. 하드코딩한 스냅샷은 적는 순간부터 썩는다.

개인정보는 레포에 두지 않는다. 전부 환경변수로 받는다.
  HH_INCOME_MONTHLY   건강보험 보수월액 (원)
  HH_ASSETS_SELF      본인 총자산 (원)
  HH_ASSETS_PARENTS   본인+부모 합산 총자산 (원, 선택)
  HH_INCOME_PARENTS   본인+부모 합산 월소득 (원, 선택)
  HH_MAX_COMMUTE_MIN  이 시간을 넘으면 브리핑에서 뺀다 (기본 90)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from datetime import date, timedelta

from geo import Commute
from sources import lh, sh, youth, gh

TODAY = date.today()


def env_won(name: str) -> int | None:
    v = os.environ.get(name, "").strip()
    v = re.sub(r"[,_원\s]", "", v)
    return int(v) if v.isdigit() else None


PROFILE = {
    "income": env_won("HH_INCOME_MONTHLY"),
    "assets": env_won("HH_ASSETS_SELF"),
    "assets_parents": env_won("HH_ASSETS_PARENTS"),
    "income_parents": env_won("HH_INCOME_PARENTS"),
    "max_commute": int(os.environ.get("HH_MAX_COMMUTE_MIN", "90")),
}


# --- 공고문에서 순위별 소득·자산 기준을 읽는다 -------------------------------

def criteria(notice: str) -> dict:
    """3순위(본인만)와 2순위(부모 합산)의 소득·자산 상한을 뽑는다.

    LH·SH 둘 다 '1인가구는 20%p 가산' 문구와 함께 1인 기준액을 표에 적어둔다.
    금액 표기가 공고마다 흔들리므로 숫자를 넓게 훑고 문맥으로 고른다.
    """
    flat = " ".join(notice.split())
    out: dict = {"raw": ""}

    # 소득 1인가구 100% 기준. 표기가 기관마다 다르다.
    #   LH: '(1인) 4,576,036원'
    #   SH: '100% 이하4,576,0366,452,8978,168,429'  (1인/2인/3인이 공백 없이 붙는다)
    # 그래서 금액 형식(X,XXX,XXX)으로 정확히 끊어야 첫 칸(1인가구)만 집힌다.
    MONEY = r"(\d{1,2},\d{3},\d{3})"
    won = [int(x.replace(",", ""))
           for x in re.findall(r"\(1인\)\s*" + MONEY, flat)]
    if not won:
        won = [int(x.replace(",", ""))
               for x in re.findall(r"100%\s*이하\s*" + MONEY, flat)]
    if not won:
        # 청년안심: '1순위 100% 3,813,363 ... / 3순위 120% 4,576,036 ...'
        # 각 % 뒤 첫 칸이 1인가구. 3순위(120%)가 상한이다. 뒤 칸(2·3인)은 더 크니
        # max() 로 뭉뚱그리면 안 되고, % 별 첫 칸만 잡아 그중 최대를 쓴다.
        won = [int(x.replace(",", ""))
               for x in re.findall(r"1[012]0%\s*" + MONEY, flat)]
    if won:
        out["income_limit"] = max(won)

    # 자산. LH: '총자산 25,100만원 이하' / SH: '총자산가액: 25,100만원 이하'
    man = [int(x.replace(",", "")) * 10_000
           for x in re.findall(r"총자산(?:가액)?\s*:?\s*([\d,]{3,8})\s*만원\s*이하", flat)]
    if man:
        out["assets_self_limit"] = min(man)      # 3순위(행복주택 청년) 기준이 더 빡빡하다
        out["assets_parents_limit"] = max(man)   # 2순위(국민임대) 기준

    m = re.search(r"3순위[^.]{0,120}?100%\s*이하", flat)
    if m:
        out["raw"] = m.group(0)[:120]
    return out


DATE_TIME = (r"['‘’]?(\d{2,4})?\.?\s*(\d{1,2})\.\s*(\d{1,2})\.\s*"
             r"\([월화수목금토일]\)\s*(\d{1,2}:\d{2})")


def apply_period(notice: str) -> tuple[str, str]:
    """공고문에서 접수 시작일과 마감일을 읽는다. -> ('2026.07.13', '2026.07.15')

    SH 목록은 게시일만 주고 접수기간을 안 준다. LH 목록은 마감일은 주지만 시작일이
    없다. 둘 다 공고문 일정표에는 있다.

    표기가 추출기마다 다르다. 시각(10:00 ~ 17:00)이 붙은 날짜 범위는 접수 일정뿐이라
    그걸 앵커로 잡는다.
      pypdf:     신청접수(온라인)'26. 7. 13.(월)10:00 ~'26. 7. 15.(수)17:00
      pdftotext: 인터넷청약 신청접수: 2026. 7. 13.(월) 10:00 ~ 7. 15.(수) 17:00
                 (끝 날짜에 연도가 없다)
    """
    flat = " ".join(notice.split())
    m = re.search(DATE_TIME + r"\s*~\s*" + DATE_TIME, flat)
    if not m:
        return "", ""
    y1, m1, d1, _, y2, m2, d2, _ = m.groups()

    def build(year, mm, dd, fallback):
        year = year or fallback
        if not year:
            return ""
        year = int(year)
        if year < 100:                  # '26 -> 2026
            year += 2000
        return f"{year}.{int(mm):02d}.{int(dd):02d}"

    return build(y1, m1, d1, y2), build(y2, m2, d2, y1)


def deadline_from_notice(notice: str) -> str:
    return apply_period(notice)[1]


def judge(crit: dict) -> list[str]:
    """순위별로 갈라서 판정한다. 2순위는 부모 자산까지 합산되므로 유불리가 뒤집힌다."""
    lines = []
    inc, assets = PROFILE["income"], PROFILE["assets"]
    il = crit.get("income_limit")
    al3 = crit.get("assets_self_limit")
    al2 = crit.get("assets_parents_limit")

    # 기준이 아예 없는 유형(청년안심 민간임대 일반공급)과 못 읽은 경우를 구분한다.
    if crit.get("no_limit"):
        return ["・ 자격: 소득·자산 기준 없음 → 통과 (자동차 소유·운행 금지)"]

    if not il:
        return ["・ 자격: 공고문에서 소득 기준을 못 읽었다 → 원문 확인 필요"]

    if inc is None:
        lines.append("・ 자격: HH_INCOME_MONTHLY 미설정 → 판정 불가")
        return lines

    ok3 = inc <= il
    mark = "통과" if ok3 else "탈락"
    lines.append(f"・ 3순위 소득: 월 {inc:,}원 / 상한 {il:,}원 → {mark}"
                 + (f" (여유 {il - inc:,}원)" if ok3 else f" ({inc - il:,}원 초과)"))
    if al3:
        if assets is None:
            lines.append(f"・ 3순위 자산: 상한 {al3:,}원 — 본인 자산 미설정, 확인 필요")
        else:
            ok = assets <= al3
            lines.append(f"・ 3순위 자산: {assets:,}원 / 상한 {al3:,}원 "
                         f"→ {'통과' if ok else '탈락'}")
    if al2 and al3 and al2 != al3:
        lines.append(f"・ 2순위는 부모 자산까지 합산({al2:,}원 상한) — 부모 자가가 있으면 위험")
    return lines


# --- 수집 -------------------------------------------------------------------

def collect(commute: Commute) -> list[dict]:
    briefs = []
    for source, name in ((lh, "LH"), (sh, "SH"), (youth, "청년안심"), (gh, "GH")):
        try:
            anns = source.relevant(source.announcements())
        except Exception as e:
            briefs.append({"agency": name, "error": f"{name} 목록 실패: {e}"})
            continue

        for ann in anns:
            try:
                # 접수기간을 채운다. 소스마다 방법이 다르다.
                #   LH·SH: 공고문 PDF 의 일정표에서 읽는다(목록엔 마감일이 없거나 게시일뿐).
                #   청년안심: 소스가 목록/상세에서 직접 준다(_period).
                # 채우기 전에 마감 판정하면 빈 deadline 이 필터를 통과해버린다.
                if hasattr(source, "_period"):
                    start, end = source._period(ann)
                    notice = None
                else:
                    notice = source.notice_text(ann)
                    start, end = apply_period(notice)
                if start:
                    ann["opens"] = start
                if not ann.get("deadline") and end:
                    ann["deadline"] = end

                if _expired(ann):
                    continue        # 이미 마감됐다

                # 자격 기준은 공고문에서 읽는다. 청년안심도 공고문 PDF 에 순위별
                # 소득표(1순위 100% / 2순위 110% / 3순위 120%)가 있다.
                crit = criteria(notice if notice is not None else source.notice_text(ann))

                us = source.units(ann)
                if not us:
                    # 매물 목록 첨부가 없는 공고가 많다. SH 특화형 매입임대(서초·강동·
                    # 금천)가 여기 걸린다. 조용히 버리면 사용자는 그런 공고가 있었다는
                    # 사실 자체를 모른다. 목록에는 띄우고 원문을 보게 한다.
                    briefs.append({"agency": name, "ann": ann, "groups": [],
                                   "total_groups": 0, "dropped": 0,
                                   "no_units": True, "criteria": crit})
                    continue
                groups = source.groups(us)
                for g in groups:
                    g["commute"] = _commute_of(g, commute)
                groups = [g for g in groups if g["commute"]]
                groups.sort(key=lambda g: g["commute"]["total_min"])
                near = [g for g in groups
                        if g["commute"]["total_min"] <= PROFILE["max_commute"]]
                if not near:
                    continue
                briefs.append({
                    "agency": name,
                    "ann": ann,
                    "groups": near[:5],
                    "total_groups": len(groups),
                    "dropped": len(source.groups(us)) - len(groups),
                    "criteria": crit,
                })
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                briefs.append({"agency": name,
                               "error": f"{ann.get('title', '?')[:40]}: {e}"})
    return briefs


def _commute_of(group: dict, commute: Commute) -> dict | None:
    """LH 주택군은 여러 건물을 묶으므로 배정 확률로 가중평균한다.
    SH 는 건물이 하나라 그냥 그 건물 값이다."""
    # 청년안심주택은 역명이 이미 있다. 지오코딩보다 정확하니 그걸 쓴다.
    if group.get("station"):
        est = commute.by_station(group["station"])
        if est:
            return {"total_min": est["total_min"], "best": est, "worst": est,
                    "certain": True}

    parts = []
    for b in group["buildings"]:
        est = commute.estimate(b["addr"])
        if est:
            parts.append((b["share"], est))
    if not parts:
        return None
    # 좌표를 못 찾은 건물은 빠지므로 남은 것들로 확률을 다시 정규화한다.
    # 안 그러면 기대값이 실제보다 작게 나온다.
    total_share = sum(share for share, _ in parts)
    parts = [(share / total_share, est) for share, est in parts]
    expected = sum(share * est["total_min"] for share, est in parts)
    best = min(parts, key=lambda p: p[1]["total_min"])[1]
    worst = max(parts, key=lambda p: p[1]["total_min"])[1]
    return {
        "total_min": round(expected),
        "best": best,
        "worst": worst,
        "certain": len(parts) == 1,
    }


# --- 브리핑 -----------------------------------------------------------------

def render(briefs: list[dict]) -> str:
    lines = [f"🏠 청약·임대 공고 브리핑 ({TODAY})", ""]
    # 날짜 표기가 기관마다 다르다. LH 는 '2026.07.13', SH 는 '2026-07-13'.
    # 문자열로 비교하면 '-' < '.' 라 SH 에는 🆕 가 절대 안 붙는다.
    yesterday = _norm(str(TODAY - timedelta(days=1)))

    real = [b for b in briefs if "ann" in b and not b.get("no_units")]
    unlisted = [b for b in briefs if b.get("no_units")]
    errors = [b for b in briefs if "error" in b]

    if not real:
        lines.append("오늘은 매물까지 확인된 공고 없음")
    for b in real:
        ann = b["ann"]
        new = "🆕 " if _norm(ann.get("posted", "")) >= yesterday else ""
        lines.append(f"📌 {new}[{b['agency']}] {ann['title'][:60]}")
        if ann.get("deadline"):
            left = _days_left(ann["deadline"])
            urgent = " ⚠️ 마감임박" if left is not None and left <= 3 else ""
            lines.append(f"・ 마감: {ann['deadline']}"
                         + (f" (D-{left}){urgent}" if left is not None else ""))
        lines += judge(b["criteria"])
        drop = f", 좌표 못 찾아 제외 {b['dropped']}개" if b.get("dropped") else ""
        lines.append(f"・ 출퇴근권 신청단위 {len(b['groups'])}개 "
                     f"(전체 {b['total_groups']}개{drop})")
        for g in b["groups"]:
            cm = g["commute"]
            rent = _range(g.get("rent_min"), g.get("rent_max"))
            dep = f"{g['deposit']:,}" if g.get("deposit") else "?"
            head = (f"   ▸ {g['unit']} · {g['count']}호 · 약 {cm['total_min']}분")
            lines.append(head)
            lines.append(f"      보증금 {dep}원 / 월 {rent}원")
            if not cm["certain"]:
                # LH: 어느 건물에 걸릴지 모른다
                b_, w_ = cm["best"], cm["worst"]
                lines.append(f"      건물 무작위: 최선 {b_['total_min']}분"
                             f"({b_['station']} 도보 {b_['walk_min']}분) ~ "
                             f"최악 {w_['total_min']}분")
            else:
                e = cm["best"]
                lines.append(f"      {e['station']}역 도보 {e['walk_min']}분"
                             + (" · 버스 필요" if e["needs_bus"] else ""))
        lines.append("")

    if unlisted:
        # 매물을 못 뽑은 공고. 조용히 버리면 이런 게 있었다는 것조차 모른다.
        # 같은 유형(특화형 매입임대 등)이 여러 건이라 유형별로 묶어 한 줄씩만 낸다.
        seen = set()
        rows = []
        for b in unlisted:
            title = b["ann"]["title"]
            key = re.sub(r"\(운영기관.*?\)|_\S+|\d{6,}|\[.*?\]", "", title).strip()
            key = (b["agency"], re.sub(r"\s+", " ", key)[:26])
            if key in seen:
                continue
            seen.add(key)
            left = _days_left(b["ann"].get("deadline", ""))
            rows.append((left if left is not None else 999,
                         f"・ [{b['agency']}] {key[1]}"
                         + (f" (D-{left})" if left is not None else "")))
        if rows:
            lines.append("📋 매물 미확인 — 공고문 직접 확인")
            for _, r in sorted(rows)[:6]:
                lines.append(r)
            lines.append("")

    # 다가오는 일정. 예측이 아니라 공고문에 적힌 실제 날짜만 쓴다.
    # 접수가 아직 시작 안 됐거나(D-day 전), 마감이 코앞인 것.
    upcoming = []
    for b in briefs:
        ann = b.get("ann")
        if not ann:
            continue
        opens, ends = ann.get("opens", ""), ann.get("deadline", "")
        d_open, d_end = _days_left(opens), _days_left(ends)
        if d_open is not None and d_open > 0:
            upcoming.append((d_open, f"・ {opens[5:]} 접수 시작 (D-{d_open}) "
                                     f"[{b['agency']}] {ann['title'][:38]}"))
        elif d_end is not None and 0 <= d_end <= 7:
            upcoming.append((d_end + 100, f"・ {ends[5:]} 마감 (D-{d_end}) "
                                          f"[{b['agency']}] {ann['title'][:38]}"))
    if upcoming:
        lines.append("📅 다가오는 일정")
        for _, line in sorted(upcoming)[:6]:
            lines.append(line)
        lines.append("")

    if errors:
        lines.append("⚠️ 수집 실패")
        for e in errors:
            lines.append(f"・ {e['error'][:90]}")
    return "\n".join(lines)


def _norm(d: str) -> str:
    """날짜 표기 통일. LH '2026.07.13' / SH '2026-07-13' 를 같이 비교하려면 필요하다."""
    return d.replace("-", ".").strip()


def _range(lo, hi) -> str:
    """월세가 없으면 0원을 찍지 않는다. 0원은 명백히 틀린 출력이다."""
    if lo is None and hi is None:
        return "확인 필요"
    if lo is None or hi is None:
        return f"{(lo or hi):,}"
    return f"{lo:,}" if lo == hi else f"{lo:,}~{hi:,}"


def _days_left(deadline: str) -> int | None:
    m = re.match(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", deadline)
    if not m:
        return None
    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (d - TODAY).days


def _expired(ann: dict) -> bool:
    """마감이 지났으면 True. 마감일을 아예 모르면(둘 다 못 읽음) 버리지 않는다 —
    확인 필요로 남겨 사용자가 직접 보게 한다."""
    left = _days_left(ann.get("deadline", ""))
    return left is not None and left < 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="수집만 하고 진단 출력")
    args = ap.parse_args()

    commute = Commute()
    briefs = collect(commute)
    text = render(briefs)
    print(text)

    if args.dry_run:
        print("\n--- 진단 ---", file=sys.stderr)
        print(f"프로필: {PROFILE}", file=sys.stderr)
        print(f"공고 {len([b for b in briefs if 'ann' in b])}건, "
              f"실패 {len([b for b in briefs if 'error' in b])}건", file=sys.stderr)


if __name__ == "__main__":
    main()
