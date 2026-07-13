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
from sources import lh, sh

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


def deadline_from_notice(notice: str) -> str:
    """SH 목록에는 접수기간이 없다. 게시일만 준다. 공고문 일정표에서 마감일을 읽는다.

    공고문 표기: 신청접수(온라인)'26. 7. 13.(월)10:00 ~'26. 7. 15.(수)17:00
    이걸 못 읽으면 마감 경고가 안 뜨고, 마감을 놓친다.
    """
    flat = " ".join(notice.split())
    m = re.search(
        r"신청접수.{0,20}?['‘’](\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\..{0,12}?"
        r"~\s*['‘’]?(\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\.",
        flat)
    if not m:
        return ""
    yy, mm, dd = m.group(4), m.group(5), m.group(6)
    return f"20{yy}.{int(mm):02d}.{int(dd):02d}"


def judge(crit: dict) -> list[str]:
    """순위별로 갈라서 판정한다. 2순위는 부모 자산까지 합산되므로 유불리가 뒤집힌다."""
    lines = []
    inc, assets = PROFILE["income"], PROFILE["assets"]
    il = crit.get("income_limit")
    al3 = crit.get("assets_self_limit")
    al2 = crit.get("assets_parents_limit")

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
    for source, name in ((lh, "LH"), (sh, "SH")):
        try:
            anns = source.relevant(source.announcements())
        except Exception as e:
            briefs.append({"agency": name, "error": f"{name} 목록 실패: {e}"})
            continue

        for ann in anns:
            try:
                us = source.units(ann)
                if not us:
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
                notice = source.notice_text(ann)
                if not ann.get("deadline"):
                    ann["deadline"] = deadline_from_notice(notice)
                briefs.append({
                    "agency": name,
                    "ann": ann,
                    "groups": near[:5],
                    "total_groups": len(groups),
                    "dropped": len(source.groups(us)) - len(groups),
                    "criteria": criteria(notice),
                })
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                briefs.append({"agency": name,
                               "error": f"{ann.get('title', '?')[:40]}: {e}"})
    return briefs


def _commute_of(group: dict, commute: Commute) -> dict | None:
    """LH 주택군은 여러 건물을 묶으므로 배정 확률로 가중평균한다.
    SH 는 건물이 하나라 그냥 그 건물 값이다."""
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
    yesterday = (TODAY - timedelta(days=1)).strftime("%Y.%m.%d")

    real = [b for b in briefs if "ann" in b]
    errors = [b for b in briefs if "error" in b]

    if not real:
        lines.append("오늘은 신청 가능한 공고 없음")
    for b in real:
        ann = b["ann"]
        new = "🆕 " if ann.get("posted", "") >= yesterday else ""
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

    if errors:
        lines.append("⚠️ 수집 실패")
        for e in errors:
            lines.append(f"・ {e['error'][:90]}")
    return "\n".join(lines)


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
