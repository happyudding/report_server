"""웹 Rawdata CSV 다운로드 E2E — GET .../web_report/rawdata_csv{,_all}.

실행:
    python tests/test_rawdata_csv.py

Honey 없이 웹에서 세션 rawdata 원본을 받는 조회 전용 경로를 고정한다:

  (a) 내용 — 저장된 parquet 과 셀 단위 완전 일치(메타 6행 TSEQ~LOLIM 포함, 문자 그대로).
      선두 UTF-8 BOM(Excel 이 한글을 안 깨고 여는 조건).
  (b) source 선택 — idx 마다 그 source 의 내용이 나오고 파일명에 source 이름이 실린다.
  (c) 오류 — 범위 밖 idx 는 404, 정수가 아니면 400.
  (d) ETag — 같은 ETag 로 다시 물으면 304 + 본문 0바이트.
  (e) 접근제어 — 비공개 세션은 신원 없는 브라우저에 404 (조회 자체를 숨긴다).
      공개 세션은 신원 없이도 받을 수 있다(읽기 전용 사용자 다운로드 허용).

전체 일괄 다운로드(rawdata_csv_all — zip 안에 source별 CSV):

  (f) zip 내용 — 열리고(CRC 정상) source 개수만큼 들어 있으며, 각 CSV 가 개별
      다운로드와 같은 내용·같은 파일명 규칙. 비ASCII 이름은 UTF-8 플래그(bit 11).
  (g) 스트리밍 — Content-Length 없음 + 응답 청크가 2개 이상(메모리 전량 적재 아님).
  (h) Content-Disposition — ascii 폴백 + filename*(RFC5987).
  (i) ETag — ':all' 로 개별 source 와 구분되고 재요청은 304 + 본문 0바이트.
  (j) 접근제어 — 개별 CSV 와 같은 가드(비공개 무신원 404 / 업로더 200).
  (k) 내부 파일명 중복 — 같은 이름 source 가 둘이면 _2 로 갈라 덮어쓰기 유실을 막는다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="rawdata_csv_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 워커 오프로드 없이 인라인 계산

import pandas as pd  # noqa: E402
from flask import Flask  # noqa: E402

import storage_gateway  # noqa: E402
from database import report_db  # noqa: E402
from report.report_extension import report_bp  # noqa: E402
from web_report.honeyform import META_COLUMNS, encode_honeyform_parquet  # noqa: E402
from web_report.validation import canon  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

USER = "tester"
UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
SID, AKEY = "s-rawcsv", "c" * 64
# 두 번째 소스 이름은 비ASCII — Content-Disposition 의 filename*(RFC5987) 경로를 태운다.
SOURCE_NAMES = ["Lot1", "웨이퍼2"]
URL = f"/pe/report/session/{SID}/web_report/rawdata_csv"
URL_ALL = f"/pe/report/session/{SID}/web_report/rawdata_csv_all"
# 내부 파일명 중복(같은 source 이름 2개) 전용 세션 — (k).
SID_DUP, AKEY_DUP = "s-rawcsv-dup", "d" * 64


def _make_parquet(offset, n_rows=10):
    """소스 1개. offset 으로 소스마다 값을 다르게 만든다.

    n_rows 를 export_source_csv 의 청크 크기(최대 1000행)보다 크게 주면 batch 를 여러 번
    돌리는 경로가 걸린다 — 헤더 중복·행 누락이 생긴다면 거기서 생긴다.
    """
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 12, 12],
        ["LOLIM", "", "", "", "", "", "", 8, 8],
    ]
    for i in range(n_rows):
        rows.append([f"s{i}", 1, 1, i % 5, i // 5, 1, "",
                     10 + offset + i * 0.1, 9 + offset + i * 0.1])
    return encode_honeyform_parquet(pd.DataFrame(rows, columns=cols))


# source 0 은 청크 2회 이상(>1000행), source 1 은 작게 — 두 경로를 한 번에 본다.
_N_ROWS_0 = 2500


def _make_session(sid, akey, blobs, source_names):
    """web_report 세션 1건 + parquet/manifest 저장. content_hash 를 돌려준다."""
    chash = hashlib.sha256(
        canon({"files": [hashlib.sha256(b).hexdigest() for b in blobs]})).hexdigest()
    report_db.create_session(sid, "x.parquet", None, product_type="MDDI", lot_id="LOT1",
                             product="P1", source="web_report", uploaded_by=USER)
    report_db.update_session(sid, analysis_key=akey, content_hash=chash, status="done")
    storage_gateway.save_webreport_sources(
        akey, chash, blobs,
        {"sources": [{"name": n, "file_name": f"{n}.csv"} for n in source_names],
         "selected_items": [], "mode": "Normal"},
        upload_root=UPLOAD_ROOT)
    return chash


def _setup():
    blobs = [_make_parquet(0, _N_ROWS_0), _make_parquet(0.5)]
    return blobs, _make_session(SID, AKEY, blobs, SOURCE_NAMES)


def _honey_headers():
    return {"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}"}


def _csv_rows(body):
    """응답 bytes → CSV 행 리스트 (BOM 검증 포함)."""
    assert body[:3] == b"\xef\xbb\xbf", f"UTF-8 BOM 이 없다: {body[:8]!r}"
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


def _expected_rows(blob):
    """parquet 에 저장된 문자열 그대로 — CSV 가 이것과 완전히 같아야 한다."""
    frame = pd.read_parquet(io.BytesIO(blob), engine="pyarrow")
    out = [list(frame.columns)]
    for row in frame.itertuples(index=False, name=None):
        out.append(["" if pd.isna(v) else str(v) for v in row])
    return out


def main():
    blobs, chash = _setup()

    # (a) 내용 — parquet 저장본과 셀 단위 완전 일치.
    r = client.get(f"{URL}?source=0")
    assert r.status_code == 200, (r.status_code, r.data[:300])
    assert r.mimetype == "text/csv", r.mimetype
    rows = _csv_rows(r.data)
    assert rows == _expected_rows(blobs[0]), "CSV 가 parquet 원본과 다르다"
    assert rows[0][:len(META_COLUMNS)] == META_COLUMNS, rows[0]
    assert [row[0] for row in rows[1:7]] == \
        ["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"], "메타 6행이 안 실렸다"
    assert rows[7][0] == "s0" and len(rows) == 1 + 6 + _N_ROWS_0, f"행 수 이상: {len(rows)}"
    assert rows[-1][0] == f"s{_N_ROWS_0 - 1}", f"마지막 행이 잘렸다: {rows[-1][:2]}"
    print(f"(a) 내용 일치 — 헤더 1 + 메타 6 + 데이터 {_N_ROWS_0}행(청크 여러 번), "
          f"BOM 있음 ({len(r.data)}바이트)")

    # (b) source 선택 — idx 마다 다른 내용, 파일명에 source 이름.
    r1 = client.get(f"{URL}?source=1")
    assert r1.status_code == 200, (r1.status_code, r1.data[:300])
    assert _csv_rows(r1.data) == _expected_rows(blobs[1]), "source=1 내용이 다르다"
    assert _csv_rows(r1.data) != rows, "source 0/1 이 같은 내용을 준다"
    disp = r1.headers.get("Content-Disposition", "")
    assert f'filename="rawdata_{SID}_src1.csv"' in disp, disp
    assert quote(f"rawdata_LOT1_{SOURCE_NAMES[1]}.csv") in disp, disp
    print(f"(b) source 선택 OK — Content-Disposition: {disp}")

    # (c) 오류 — 범위 밖 404 / 정수 아님 400.
    assert client.get(f"{URL}?source=2").status_code == 404
    assert client.get(f"{URL}?source=-1").status_code == 404
    assert client.get(f"{URL}?source=abc").status_code == 400
    print("(c) 범위 밖 404 · 정수 아님 400")

    # (d) ETag — 같은 값으로 재요청하면 304 + 본문 없음.
    etag = r.headers.get("ETag")
    assert etag == f'"{chash}:src0"', etag
    r304 = client.get(f"{URL}?source=0", headers={"If-None-Match": etag})
    assert r304.status_code == 304, r304.status_code
    assert not r304.data, f"304 인데 본문이 있다: {len(r304.data)}바이트"
    # source 가 다르면 ETag 도 달라야 한다(같으면 옛 source 를 캐시에서 준다).
    assert r1.headers.get("ETag") != etag, "source 별 ETag 가 같다"
    print(f"(d) ETag 304 OK — {etag}")

    # (e) 접근제어 — 공개는 무신원 허용(위 (a)~(d) 가 전부 무신원), 비공개는 404.
    report_db.update_session(SID, is_private=1)
    assert client.get(f"{URL}?source=0").status_code == 404, "비공개인데 무신원에 내려줬다"
    r_owner = client.get(f"{URL}?source=0", headers=_honey_headers())
    assert r_owner.status_code == 200, (r_owner.status_code, r_owner.data[:300])
    report_db.update_session(SID, is_private=0)
    print("(e) 비공개 세션 무신원 404 · 업로더 200")

    # (f) 전체 zip — 열리고, source 개수만큼 들어 있고, 각 내용이 개별 CSV 와 같다.
    rz = client.get(URL_ALL)
    assert rz.status_code == 200, (rz.status_code, rz.data[:300])
    assert rz.mimetype == "application/zip", rz.mimetype
    z = zipfile.ZipFile(io.BytesIO(rz.data))
    assert z.testzip() is None, "zip CRC 불일치 — 스트리밍 중 깨졌다"
    names = z.namelist()
    assert len(names) == len(SOURCE_NAMES), names
    for i, sname in enumerate(SOURCE_NAMES):
        member = f"rawdata_LOT1_{sname}.csv"      # 단일 다운로드 파일명과 같은 규칙
        assert member in names, (member, names)
        assert _csv_rows(z.read(member)) == _expected_rows(blobs[i]), member
    # 비ASCII 이름은 UTF-8 플래그(bit 11)가 서야 탐색기·7-Zip 이 안 깨고 푼다.
    assert z.getinfo(f"rawdata_LOT1_{SOURCE_NAMES[1]}.csv").flag_bits & 0x800, "UTF-8 플래그 없음"
    print(f"(f) 전체 zip OK — {names} ({len(rz.data)}바이트)")

    # (g) 스트리밍 — Content-Length 없이 청크로 나온다(= zip 전량을 메모리에 안 만든다).
    rz_s = client.get(URL_ALL)
    assert rz_s.headers.get("Content-Length") is None, rz_s.headers
    n_chunks = sum(1 for _ in rz_s.response)
    assert n_chunks > 1, f"청크가 {n_chunks}개 — 스트리밍이 아니다"
    print(f"(g) 스트리밍 OK — Content-Length 없음, 청크 {n_chunks}개")

    # (h) Content-Disposition — ascii 폴백 + filename*(RFC5987).
    disp_all = rz.headers.get("Content-Disposition", "")
    assert f'filename="rawdata_{SID}_all.csv.zip"' in disp_all, disp_all
    assert quote("rawdata_LOT1_all.csv.zip") in disp_all, disp_all
    print(f"(h) Content-Disposition: {disp_all}")

    # (i) ETag — ':all' 로 개별 source 와 구분되고, 재요청은 304 + 본문 0바이트.
    etag_all = rz.headers.get("ETag")
    assert etag_all == f'"{chash}:all"', etag_all
    assert etag_all != etag, "전체 zip 과 source 0 의 ETag 가 같다"
    rz304 = client.get(URL_ALL, headers={"If-None-Match": etag_all})
    assert rz304.status_code == 304, rz304.status_code
    assert not rz304.data, f"304 인데 본문이 있다: {len(rz304.data)}바이트"
    print(f"(i) ETag 304 OK — {etag_all}")

    # (j) 접근제어 — 개별 CSV 와 같은 가드.
    report_db.update_session(SID, is_private=1)
    assert client.get(URL_ALL).status_code == 404, "비공개인데 무신원에 내려줬다"
    assert client.get(URL_ALL, headers=_honey_headers()).status_code == 200
    report_db.update_session(SID, is_private=0)
    print("(j) 전체 zip 비공개 무신원 404 · 업로더 200")

    # (k) 내부 파일명 중복 — 같은 이름 source 2개가 서로 덮어쓰지 않는다.
    dup_blobs = [_make_parquet(1), _make_parquet(2)]
    _make_session(SID_DUP, AKEY_DUP, dup_blobs, ["같은이름", "같은이름"])
    rd = client.get(f"/pe/report/session/{SID_DUP}/web_report/rawdata_csv_all")
    assert rd.status_code == 200, (rd.status_code, rd.data[:300])
    zd = zipfile.ZipFile(io.BytesIO(rd.data))
    dnames = zd.namelist()
    assert len(dnames) == 2 and len(set(dnames)) == 2, f"중복 이름이 안 갈렸다: {dnames}"
    assert dnames == ["rawdata_LOT1_같은이름.csv", "rawdata_LOT1_같은이름_2.csv"], dnames
    for name, blob in zip(dnames, dup_blobs):
        assert _csv_rows(zd.read(name)) == _expected_rows(blob), name
    print(f"(k) 내부 파일명 중복 dedup OK — {dnames}")

    print("\nPASS — rawdata CSV 다운로드 (개별: 내용·source 선택·오류·ETag·접근제어 / "
          "전체 zip: 내용·스트리밍·파일명·ETag·접근제어·중복 dedup)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
