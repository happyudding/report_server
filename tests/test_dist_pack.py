"""Distribution pack(클라 정렬 전가) 검증 — 값 일치 / 폐기·폴백 / 무효화.

실행:
    python tests/test_dist_pack.py

검증 항목:
  (a) pack 없이 만든 세션(= 기존 세션)은 종전 경로로 그대로 열린다.
  (b) pack 을 첨부하면 ingest 가 영구 저장하고(dist_pack_saved), 전체/Bin1/배치 응답이
      **폴백 계산과 정준 JSON 으로 완전히 같다**.
  (c) pack 세션은 서버가 ECDF 를 다시 정렬하지 않는다
      (build_distribution_compact 를 폭파시켜도 응답이 나온다).
  (d) 손상 chunk / 미지 index 포맷은 폐기되고 기존 계산 폴백으로 열린다.
  (e) 전처리(항목 제외·outlier) 세션은 업로드된 pack 을 쓰지 않고 폴백 계산한다 — pack 은
      업로드 시점(전처리 없음) 기준이라 값이 다르기 때문.
  (f) raw 편집으로 content_hash 가 바뀌면 구 pack 이 조회되지 않고 디렉토리도 회수된다.
  (g) 서버 재시작·캐시 전멸 후에도 pack 으로 응답한다(영구 저장 — 캐시가 아님).
  (h) 전처리 세션용 variant 를 서버가 1회 만들면(materialize_dist_pack) 이후 조회는
      재정렬 없이 **폴백 계산과 같은 값**을 낸다.
  (i) variant 생성은 원본 pack 을 건드리지 않고, 전처리를 해제하면 원본 pack 으로 복귀한다.
  (j) 전처리 spec 을 바꾸면 구 digest variant 가 회수되고 새 digest 로 다시 만들어진다.
  (k) 웹 셀 편집 후 서버가 새 세대 base pack 을 다시 만들고 프리웜을 예약한다.
  (l) chunk 디코드 결과 캐시 — 같은 chunk 재조회 시 파일을 다시 읽지 않고, 캐시 유/무
      응답이 정준 JSON 으로 완전히 같다. akey 무효화면 다시 읽는다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="wr_distpack_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 인라인 — 테스트 결정성

from database import report_db  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import compute as wr_compute  # noqa: E402
from web_report import dist_pack, dist_pack_store  # noqa: E402
from web_report import edits as wr_edits  # noqa: E402
from web_report import ingest as wr_ingest  # noqa: E402
from web_report import preprocess as wr_preprocess  # noqa: E402
from web_report import response_cache  # noqa: E402
from web_report import service as wr_service  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, decode_split_honeyform_parquet,
    encode_honeyform_parquet,
)
from web_report.tabs import distribution as dist_tab  # noqa: E402

# report_extension 이 storage 포트를 주입한다 — 라우트를 안 쓰더라도 필요.
from flask import Flask  # noqa: E402

from report.report_extension import report_bp, init_app as init_report  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
init_report(app)
report_db.init_report_db()

UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
N_ITEMS = 7
N_ROWS = 40

_failures = []


def check(ok, label):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        _failures.append(label)


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_parquet(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    items = [f"IT{j:02d}" for j in range(N_ITEMS)]
    rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row[META_COLUMNS[0]] = label
        for j, it in enumerate(items):
            row[it] = {"TSEQ": N_ITEMS - j, "TNO": 1000 + j,
                       "STEP": ("P1", "P2")[j % 2], "UNIT": "V",
                       "HILIM": 3.0, "LOLIM": -3.0}[label]
        rows.append(row)
    for i in range(N_ROWS):
        bin_v = 1 if i % 4 else 2
        row = {"SERIAL": f"S{i:04d}", "SHOT": 0, "DUT": i % 2,
               "XPOS": i % 6, "YPOS": i // 6, "BIN": bin_v,
               "FAILTNO": "" if bin_v == 1 else 1000 + (i % N_ITEMS)}
        for j, it in enumerate(items):
            if j == 0:
                row[it] = int(rng.integers(-5, 5))          # 정수 전용 컬럼
            elif j == 1:
                row[it] = round(float(rng.integers(-3, 3)), 1)   # 중복 많은 이산값
            elif j == 2:
                row[it] = 1.5                                # 전부 같은 값
            else:
                row[it] = round(float(rng.normal(0, 2)), 4)
        rows.append(row)
    df = pd.DataFrame(rows, columns=META_COLUMNS + items)
    return encode_honeyform_parquet(df)


def build_client_pack(files, selected_items, mode):
    """Honey 클라가 하는 일 그대로 — 업로드할 parquet bytes 를 디코드해 pack 생성."""
    tables = [decode_split_honeyform_parquet(
        f["data"], source=f["name"], file_name=f["filename"], keep_df=False)
        for f in files]
    index, chunk_iter = dist_pack.build_dist_pack(
        tables, selected_items, mode, chunk_items=3)
    chunks = {cid: dist_pack.gzip_pack_chunk(c, level=1) for cid, c in chunk_iter}
    return {"index": dist_pack.dumps_pack_index(index), "chunks": chunks}


def make_files(n=2):
    return [{"name": f"Lot{i}", "filename": f"Lot{i}.csv", "data": make_parquet(i)}
            for i in range(n)]


def create_session(*, with_pack: bool, files=None, mode="Normal", mutate_pack=None):
    files = files or make_files()
    manifest = {
        "meta": {"product_type": "MDDI", "product": "P1", "lot_id": "L1"},
        "mode": mode,
        "sources": [{"name": f["name"], "file_name": f["filename"]} for f in files],
        "selected_items": [],
        "client": {"user": "tester", "host": "testhost"},
    }
    pack = build_client_pack(files, [], mode) if with_pack else None
    if pack and mutate_pack:
        pack = mutate_pack(pack)
    result = wr_ingest.ingest_webreport(
        manifest, files, report_db=report_db, upload_root=UPLOAD_ROOT,
        client_ip="127.0.0.1", user_agent="Mozilla/5.0 HoneyUser/tester",
        dist_pack=pack)
    return result


def expected_compact(files, *, bin1=False, only=None, mode="Normal"):
    """pack 을 쓰지 않는 기존 서버 계산 결과 (비교 기준)."""
    from web_report.dist_blob import compute_dist_compact

    tables = [decode_split_honeyform_parquet(
        f["data"], source=f["name"], file_name=f["filename"], keep_df=False)
        for f in files]
    return compute_dist_compact(tables, [], mode, bin1=bin1, only=only)


def expected_prep_compact(files, session_id, *, bin1=False, only=None, mode="Normal"):
    """전처리를 적용한 폴백 계산 결과 (비교 기준).

    loader 와 같은 순서(전처리 적용 → compute_dist_compact)로, 세션에 **저장된** spec 을
    그대로 쓴다 — 서버가 조회할 때 하는 일과 동일하다.
    """
    from web_report.dist_blob import compute_dist_compact

    tables = [decode_split_honeyform_parquet(
        f["data"], source=f["name"], file_name=f["filename"], keep_df=False)
        for f in files]
    spec = wr_edits.load_preprocess(report_db, session_id)
    tables, _ = wr_preprocess.apply_tables(tables, spec)
    return compute_dist_compact(tables, [], mode, bin1=bin1, only=only)


def variant_dir(session_id, *, prep_digest=""):
    session = report_db.get_session(session_id)
    return dist_pack_store.pack_dir(
        UPLOAD_ROOT, session["analysis_key"], session["content_hash"], "Normal",
        prep_digest)


def wait_for(cond, timeout=60.0):
    """백그라운드 잡(단일 소비자 스레드) 완료 대기 — 성공 여부 반환."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def served(session_id, *, bin1=False, subjects=None):
    if subjects is None:
        return wr_service.get_distribution(
            session_id, report_db=report_db, upload_root=UPLOAD_ROOT, bin1=bin1)
    return wr_service.get_distribution_batch(
        session_id, subjects, report_db=report_db, upload_root=UPLOAD_ROOT, bin1=bin1)


def clear_caches():
    """서버 재시작 상당 — 인메모리 캐시 전멸 + dist 디스크 캐시 삭제.

    pack 이 '캐시가 아니라 영구 데이터'임을 증명하려면 캐시 쪽만 지워야 한다.
    """
    for name in dir(wr_cache):
        obj = getattr(wr_cache, name)
        if name.endswith("_CACHE") and hasattr(obj, "clear"):
            obj.clear()
    for cache_dir in (UPLOAD_ROOT / "web_report").glob("*/cache"):
        shutil.rmtree(cache_dir, ignore_errors=True)


# ── (a) pack 없는 세션 = 기존 동작 ───────────────────────────────────────────
files = make_files()
plain = create_session(with_pack=False, files=files)
check(plain.get("dist_pack_saved") is False, "(a) pack 미첨부 → dist_pack_saved=False")
check(canon(served(plain["session_id"])) == canon(expected_compact(files)),
      "(a) pack 없는 기존 세션이 종전 값 그대로")

# ── (b) pack 첨부 → 저장 + 값 완전 일치 ──────────────────────────────────────
packed = create_session(with_pack=True, files=files)
sid = packed["session_id"]
check(packed.get("dist_pack_saved") is True, "(b) pack 첨부 → dist_pack_saved=True")
check(canon(served(sid)) == canon(expected_compact(files)),
      "(b) 전체 Distribution 정준 JSON 일치")
check(canon(served(sid, bin1=True)) == canon(expected_compact(files, bin1=True)),
      "(b) Bin1 Distribution 정준 JSON 일치")
subjects = ["IT01", "IT03", "IT05"]
check(canon(served(sid, subjects=subjects))
      == canon(expected_compact(files, only=subjects)),
      "(b) 배치(distribution_batch) 정준 JSON 일치")
check(canon(served(sid, subjects=subjects, bin1=True))
      == canon(expected_compact(files, only=subjects, bin1=True)),
      "(b) 배치 Bin1 정준 JSON 일치")

# ── (c) pack 세션은 서버가 재정렬하지 않는다 ────────────────────────────────
_real_compact = dist_tab.build_distribution_compact


def _boom(*a, **kw):
    raise AssertionError("서버가 ECDF 를 재정렬했다 (pack 경로가 아님)")


# 기대값은 **패치 전에** 계산해 둔다 — 비교 자체가 폴백 계산을 부르면 의미가 없다.
_want_full = canon(expected_compact(files))
_want_batch = canon(expected_compact(files, only=subjects))
clear_caches()
dist_tab.build_distribution_compact = _boom
try:
    got_full = canon(served(sid))
    got_batch = canon(served(sid, subjects=subjects))
    check(got_full == _want_full,
          "(c) 재정렬 없이 전체 응답 (build_distribution_compact 미호출)")
    check(got_batch == _want_batch, "(c) 재정렬 없이 배치 응답")
except AssertionError as exc:
    check(False, f"(c) {exc}")
finally:
    dist_tab.build_distribution_compact = _real_compact

# ── (g) 캐시 전멸(재시작 상당) 후에도 pack 으로 응답 ────────────────────────
clear_caches()
check(canon(served(sid)) == canon(expected_compact(files)),
      "(g) 캐시 전멸 후에도 pack 으로 응답 (영구 저장)")

# ── (l) chunk 디코드 캐시 — 값 동일 + 파일 재읽기 없음 (2026-07-28) ──────────
# distribution_batch 는 요청마다 chunk 를 read+gunzip+json.loads 했다(대형 세션은
# chunk 1개가 비압축 수십 MB — 순수 GIL 점유). 디코드 결과를 캐시해 첫 1회로 줄인다.
_want_batch_l = canon(expected_compact(files, only=subjects))
clear_caches()
_real_read = Path.read_bytes
_reads = {"n": 0}


def _counting_read(self):
    if self.name.startswith("chunk_"):
        _reads["n"] += 1
    return _real_read(self)


Path.read_bytes = _counting_read
try:
    got1 = canon(served(sid, subjects=subjects))
    first_reads = _reads["n"]
    response_cache._DIST_BATCH_CACHE.clear()   # 응답 gzip 캐시를 비워 재계산을 강제
    wr_cache.DIST_CACHE.clear()
    got2 = canon(served(sid, subjects=subjects))
    check(first_reads > 0, f"(l) 첫 조회는 chunk 파일을 읽는다 ({first_reads}회)")
    check(_reads["n"] == first_reads,
          f"(l) 두 번째 조회는 chunk 파일을 다시 읽지 않는다 ({_reads['n']}회)")
    check(got1 == _want_batch_l and got2 == _want_batch_l,
          "(l) 캐시 유/무 응답 정준 JSON 완전 일치")
finally:
    Path.read_bytes = _real_read

wr_cache.evict_akey_caches(report_db.get_session(sid)["analysis_key"])
check(not wr_cache.DIST_CHUNK_CACHE, "(l) akey 무효화가 chunk 캐시를 회수")
Path.read_bytes = _counting_read
try:
    before = _reads["n"]
    response_cache._DIST_BATCH_CACHE.clear()
    check(canon(served(sid, subjects=subjects)) == _want_batch_l,
          "(l) 무효화 후 재디코드해도 같은 값")
    check(_reads["n"] > before, "(l) 무효화 후에는 파일을 다시 읽는다")
finally:
    Path.read_bytes = _real_read

# ── (d) 손상 chunk / 미지 index 포맷 → 폐기 + 폴백 ──────────────────────────
def _corrupt_chunk(pack):
    pack = dict(pack)
    chunks = dict(pack["chunks"])
    first = sorted(chunks)[0]
    chunks[first] = b"\x1f\x8bnot really gzip"
    pack["chunks"] = chunks
    return pack


def _alien_index(pack):
    pack = dict(pack)
    idx = json.loads(pack["index"])
    idx["format"] = "dist-pack-v999"
    pack["index"] = json.dumps(idx, separators=(",", ":"))
    return pack


bad1 = create_session(with_pack=True, files=files, mutate_pack=_corrupt_chunk)
check(bad1.get("dist_pack_saved") is False, "(d) 손상 chunk → 저장 거부")
check(canon(served(bad1["session_id"])) == canon(expected_compact(files)),
      "(d) 손상 chunk 세션도 폴백 계산으로 정상 응답")

bad2 = create_session(with_pack=True, files=files, mutate_pack=_alien_index)
check(bad2.get("dist_pack_saved") is False, "(d) 미지 index 포맷 → 저장 거부")
check(canon(served(bad2["session_id"])) == canon(expected_compact(files)),
      "(d) 미지 포맷 세션도 폴백 계산으로 정상 응답")

# ── (e) 전처리 세션은 업로드된 pack 을 쓰지 않는다 ──────────────────────────
# (variant 가 아직 없는 첫 조회 — 값은 폴백 계산과 같아야 한다. 이 조회가 백그라운드
#  variant 생성을 예약하지만, 생성되든 말든 값은 동일하므로 아래 검증에 영향이 없다.)
prep_sid = create_session(with_pack=True, files=files)["session_id"]
wr_edits.save_preprocess(report_db, prep_sid, {"exclude_items": ["IT03"]})
clear_caches()
prep_out = served(prep_sid)
check("IT03" not in (prep_out.get("items") or {}),
      "(e) 전처리(항목 제외)가 반영됨 — pack 무시하고 폴백 계산")
check(canon(served(prep_sid, subjects=["IT01", "IT03"]))
      == canon(expected_compact(files, only=["IT01"])),
      "(e) 전처리 세션 배치도 폴백 계산 결과")
# 전처리를 해제하면 다시 pack 경로로 돌아온다
wr_edits.save_preprocess(report_db, prep_sid, {})
clear_caches()
check(canon(served(prep_sid)) == canon(expected_compact(files)),
      "(e) 전처리 해제 → pack 경로 복귀")

# ── (f) raw 편집(content_hash 변경) → 구 pack 미사용 + 회수 ─────────────────
edit_sid = create_session(with_pack=True, files=files)["session_id"]
edit_session = report_db.get_session(edit_sid)
akey, old_chash = edit_session["analysis_key"], edit_session["content_hash"]
pack_dir_old = dist_pack_store.pack_dir(UPLOAD_ROOT, akey, old_chash, "Normal")
check(pack_dir_old.is_dir(), "(f) 편집 전 pack 디렉토리 존재")

wr_service.edit_raw_data(
    edit_sid, report_db=report_db, upload_root=UPLOAD_ROOT,
    edits=[{"source": "Lot0", "row_idx": 0, "column": "IT03", "value": "2.75"}])
clear_caches()
check(not pack_dir_old.is_dir(), "(f) 구 세대 pack 디렉토리 회수됨")
edited = served(edit_sid)
check(canon(edited) != canon(expected_compact(files)),
      "(f) raw 편집 값이 반영됨 (구 pack 을 재사용하지 않음)")
check(2.75 in (edited["items"]["IT03"]["sources"]["Lot0"]["x"]),
      "(f) 편집한 값이 ECDF x 에 존재")

# ── (h) 전처리 variant 를 서버가 1회 생성 → 재정렬 없이 폴백과 같은 값 ──────
var_sid = create_session(with_pack=True, files=files)["session_id"]
var_spec = {"exclude_items": ["IT03"], "outlier": {"mode": "stdev", "k": 2}}
wr_edits.save_preprocess(report_db, var_sid, var_spec)
var_digest = wr_preprocess.session_digest(report_db, var_sid)
check(bool(var_digest), "(h) 전처리 digest 가 생성됨")
# 기대값은 pack 생성·패치 전에 폴백 계산으로 확보한다.
_want_prep_full = canon(expected_prep_compact(files, var_sid))
_want_prep_bin1 = canon(expected_prep_compact(files, var_sid, bin1=True))
_want_prep_batch = canon(expected_prep_compact(files, var_sid, only=subjects))

check(wr_service.materialize_dist_pack(
        var_sid, report_db=report_db, upload_root=UPLOAD_ROOT) == "saved",
      "(h) 전처리 variant 생성 성공")
check(variant_dir(var_sid, prep_digest=var_digest).is_dir(),
      "(h) variant 디렉토리 존재 (<chash>_<mode>_p<digest8>)")

clear_caches()
dist_tab.build_distribution_compact = _boom
try:
    check(canon(served(var_sid)) == _want_prep_full,
          "(h) 전처리 전체 응답이 재정렬 없이 폴백과 일치")
    check(canon(served(var_sid, bin1=True)) == _want_prep_bin1,
          "(h) 전처리 Bin1 응답이 재정렬 없이 폴백과 일치")
    check(canon(served(var_sid, subjects=subjects)) == _want_prep_batch,
          "(h) 전처리 배치 응답이 재정렬 없이 폴백과 일치")
except AssertionError as exc:
    check(False, f"(h) {exc}")
finally:
    dist_tab.build_distribution_compact = _real_compact

# ── (i) 원본 pack 불변 + 전처리 해제 시 복귀 ────────────────────────────────
check(variant_dir(var_sid).is_dir(), "(i) 원본 pack 디렉토리는 그대로")
check(wr_service.materialize_dist_pack(
        var_sid, report_db=report_db, upload_root=UPLOAD_ROOT) == "exists",
      "(i) 이미 있으면 재생성하지 않음(exists)")

wr_edits.save_preprocess(report_db, var_sid, {})
check(wr_preprocess.session_digest(report_db, var_sid) == "",
      "(i) 해제 → digest 가 빈 문자열 (기존 키로 복귀)")
check(variant_dir(var_sid).is_dir(), "(i) 해제 후에도 원본 pack 이 남아있음")
_want_base_full = canon(expected_compact(files))     # 패치 전에 확보 ((c) 와 같은 이유)
clear_caches()
dist_tab.build_distribution_compact = _boom
try:
    check(canon(served(var_sid)) == _want_base_full,
          "(i) 전처리 해제 → 재정렬 없이 원본 pack 으로 복귀")
except AssertionError as exc:
    check(False, f"(i) {exc}")
finally:
    dist_tab.build_distribution_compact = _real_compact

# ── (j) spec 변경 → 구 variant 회수 + 새 digest 로 재생성 ───────────────────
wr_edits.save_preprocess(report_db, var_sid, var_spec)          # 다시 (h) 의 spec
old_variant = variant_dir(var_sid, prep_digest=var_digest)
check(old_variant.is_dir(), "(j) 변경 전 variant 존재")
wr_service.save_preprocess(
    var_sid, report_db=report_db, upload_root=UPLOAD_ROOT,
    spec={"exclude_items": ["IT03"], "outlier": {"mode": "stdev", "k": 3}})
new_digest = wr_preprocess.session_digest(report_db, var_sid)
check(new_digest and new_digest != var_digest, "(j) spec 변경으로 digest 가 바뀜")
check(not old_variant.is_dir(), "(j) 구 digest variant 회수됨")

_want_new_spec = canon(expected_prep_compact(files, var_sid))
check(wr_service.materialize_dist_pack(
        var_sid, report_db=report_db, upload_root=UPLOAD_ROOT) in ("saved", "exists"),
      "(j) 새 digest variant 생성")
clear_caches()
dist_tab.build_distribution_compact = _boom
try:
    check(canon(served(var_sid)) == _want_new_spec,
          "(j) 새 spec 응답이 재정렬 없이 폴백과 일치")
except AssertionError as exc:
    check(False, f"(j) {exc}")
finally:
    dist_tab.build_distribution_compact = _real_compact

# ── (k) 웹 셀 편집 → 새 세대 base pack 재생성 + 프리웜 예약 ─────────────────
mk_sid = create_session(with_pack=True, files=files)["session_id"]
_prewarm_before = wr_compute.STATS["prewarm_queued"]
wr_service.edit_raw_data(
    mk_sid, report_db=report_db, upload_root=UPLOAD_ROOT,
    edits=[{"source": "Lot0", "row_idx": 1, "column": "IT04", "value": "1.25"}])
check(wr_compute.STATS["prewarm_queued"] > _prewarm_before,
      "(k) 편집 후 프리웜 예약됨")
check(wait_for(lambda: variant_dir(mk_sid).is_dir()),
      "(k) 새 세대 base pack 이 백그라운드로 재생성됨")

clear_caches()
dist_tab.build_distribution_compact = _boom
try:
    got_edit = served(mk_sid)
    check(1.25 in got_edit["items"]["IT04"]["sources"]["Lot0"]["x"],
          "(k) 재생성된 pack 에 편집 값이 반영됨 (재정렬 없음)")
except AssertionError as exc:
    check(False, f"(k) {exc}")
finally:
    dist_tab.build_distribution_compact = _real_compact

print()
if _failures:
    print(f"FAILED {len(_failures)}건: {_failures}")
    sys.exit(1)
print("ALL PASS")
