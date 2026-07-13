"""LH·SH 공통 유틸: HTTP 가져오기, xlsx/PDF 파싱.

HTTP 는 전부 curl 로 나간다. LH 인증서에 Authority Key Identifier 가 빠져 있어서
파이썬 ssl 기본 검증에 걸리는데, curl 은 통과한다.
"""
from __future__ import annotations

import atexit
import html
import io
import os
import re
import subprocess
import tempfile
import urllib.parse
import zipfile

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# LH 목록은 POST 로 필터를 걸 때 세션 쿠키를 요구한다. 프로세스 내내 유지한다.
_JAR = tempfile.NamedTemporaryFile(prefix="hh-cookies-", suffix=".txt", delete=False).name
atexit.register(lambda: os.path.exists(_JAR) and os.unlink(_JAR))


def curl(url: str, *, data: dict | None = None, referer: str = "",
         timeout: int = 60, headers: bool = False) -> bytes:
    """GET/POST. data 를 주면 POST. headers=True 면 헤더만 받고 리다이렉트를 따라가지 않는다."""
    cmd = ["curl", "-sS", "-A", UA, "--max-time", str(timeout),
           "-b", _JAR, "-c", _JAR]
    if referer:
        cmd += ["-e", referer]
    if headers:
        cmd += ["-D", "-", "-o", "/dev/null"]  # SH 첨부는 302 Location 에 실경로가 있다
    else:
        cmd += ["-L"]
    if data is not None:
        cmd += ["-X", "POST", "--data", urllib.parse.urlencode(data, encoding="utf-8")]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
    if p.returncode != 0:
        raise RuntimeError(f"curl {p.returncode}: {p.stderr.decode()[:200]}")
    return p.stdout


def text(url: str, **kw) -> str:
    return curl(url, **kw).decode("utf-8", "replace")


def strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def read_xlsx(blob: bytes) -> list[list[str]]:
    """xlsx -> 행별 셀 문자열 리스트. sharedStrings 를 풀어서 값을 채운다."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
            shared.append(html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))

    name = next(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n))
    sheet = z.read(name).decode("utf-8", "replace")

    rows = []
    for row in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        cells: dict[str, str] = {}
        # self-closing 셀(<c r="J13"/>)과 일반 셀을 한 패턴으로 가른다.
        # 예전 패턴은 [^>]* 가 self-closing 의 '/' 까지 먹어서, 빈 셀이 다음 셀의
        # 값을 빨아들이고 컬럼이 한 칸씩 밀렸다. 값이 조용히 틀리는 버그였다.
        for attrs, inner in re.findall(
                r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
            inner = inner or ""
            ref = re.search(r'r="([A-Z]+)\d+"', attrs)
            typ = re.search(r't="(\w+)"', attrs)
            inline = re.search(r"<is>.*?<t[^>]*>(.*?)</t>.*?</is>", inner, re.S)
            value = re.search(r"<v>(.*?)</v>", inner, re.S)
            if inline:
                val = html.unescape(inline.group(1))
            elif value:
                val = value.group(1)
                if typ and typ.group(1) == "s" and val.isdigit() and int(val) < len(shared):
                    val = shared[int(val)]
            else:
                val = ""
            cells[ref.group(1) if ref else ""] = val
        if cells:
            width = max((_colnum(k) for k in cells if k), default=0)
            rows.append([cells.get(_colname(i), "") for i in range(1, width + 1)])
    return rows


def _colnum(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n


def _colname(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def pdf_text(blob: bytes) -> str:
    """PDF -> 텍스트. LH·SH 공고문 모두 스캔이 아니라 텍스트 PDF다.

    pypdf 는 표의 셀 경계에 공백이 아니라 NUL(U+0000) 을 넣는다. str.split() 은
    NUL 을 공백으로 치지 않아서, 정리하지 않으면 '100%\\x00이하4,576,036' 같은
    문자열이 되어 정규식이 전부 빗나간다.
    """
    try:
        import pypdf
    except ImportError as e:
        raise RuntimeError("pypdf 가 필요하다: pip install pypdf") from e
    reader = pypdf.PdfReader(io.BytesIO(blob))
    raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", raw)


def money(v) -> int | None:
    s = re.sub(r"[,\s원]", "", str(v))
    return int(float(s)) if re.fullmatch(r"\d+(\.\d+)?", s) else None
