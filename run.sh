#!/usr/bin/env bash
# 청약 브리핑 실행 스크립트. 클라우드 routine 은 이 파일 하나만 받아 실행한다.
#   curl -sfL https://raw.githubusercontent.com/heogeon0/housing-hunt/main/run.sh | bash
# 소스 파일이 늘어도 routine 프롬프트를 안 고쳐도 되게, 받을 파일 목록을 여기서 관리한다.
set -u
REPO="https://raw.githubusercontent.com/heogeon0/housing-hunt/main"
DIR="${HH_DIR:-/tmp/hh}"
rm -rf "$DIR" && mkdir -p "$DIR/sources" && cd "$DIR" || exit 1

FILES="hunt.py geo.py stations.json requirements.txt
        sources/__init__.py sources/common.py
        sources/lh.py sources/sh.py sources/youth.py sources/gh.py"
for f in $FILES; do
  curl -sfL "$REPO/$f" -o "$f" && echo "OK  $f" || echo "FAIL $f"
done

echo "--- deps ---"
pip install -q -r requirements.txt 2>&1 | tail -2
python3 -c "import pypdf" 2>/dev/null && echo "pypdf OK" || {
  pip install -q cffi cryptography pypdf 2>&1 | tail -1
  command -v pdftotext >/dev/null || (apt-get install -y poppler-utils 2>/dev/null \
    || sudo apt-get install -y poppler-utils 2>/dev/null) | tail -1
}

echo "--- run ---"
python3 hunt.py > brief.txt 2> err.txt
echo "exit=$? / brief $(wc -c < brief.txt)B"
tail -20 err.txt >&2
cat brief.txt
