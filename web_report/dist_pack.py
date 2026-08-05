"""Distribution 정렬 pack — 서버 정렬(np.unique) 제거용 공용 빌더 (2026-07-23).

기존 dist_blob(2026-07-15)은 완성된 ECDF JSON 을 **캐시에 시딩**하는 방식이라 두 한계가
있었다: ① 캐시라 용량 상한에 걸리면 삭제돼 세션 재조회(콜드 재오픈)를 보장하지 못하고
② 항목 배치 조회(distribution_batch)는 여전히 tables 를 매번 재정렬했다.

pack 은 ECDF 계산 중 **비싼 앞단(정렬+중복 묶기)만** Honey 가 업로드 시점에 끝내 올리고,
서버는 조회 때 **덧셈(cumsum)만** 해서 기존과 완전히 동일한 ``ecdf-columnar-v1`` 응답을
만든다. 저장은 캐시가 아니라 영구 파생 데이터(dist_pack_store)라 재시작·재조회에도 살아
있다. 프런트는 응답 형식이 같아 무변경이다.

pack 이 저장하는 것 (항목·소스별):
- ``x``  : ``np.round(고유값, 6)`` — 오늘 compact 의 x 와 같은 배열
- ``c``  : 고유값별 전체 count (**반올림 전** 고유값 기준)
- ``c1`` : 같은 위치의 bin1(양품 BIN==PASS_BIN **그리고** 규격 [LSL,USL] 내) count

y(누적%)는 저장하지 않는다 — count 에서 바로 만들 수 있고, bin1 도 ``c1>0`` 행만 골라
같은 방법으로 만들면 "필터 후 unique" 와 집합·순서가 수학적으로 동일하기 때문이다.

주의: ``ecdf_from_pack_items`` 결과는 서버 폴백 계산
(``tabs.distribution.build_distribution_compact``)과 정준 JSON 으로 완전 일치해야 한다.
항목 정렬(사전순)·소스 순서·반올림 순서를 바꾸면 그 일치가 깨진다. 이 모듈은 캐시·저장소·
DB 에 의존하지 않는 순수 모듈이라 Honey 클라이언트가 그대로 import 한다(dist_blob 과 동일).
"""
from __future__ import annotations

import gzip
import io
import json

import numpy as np

# ⚠️ 세대(리비전) 토큰. 값 계산 로직(정렬·count·bin1 필터·반올림 순서)을 바꾸면 아래 두
# 버전 문자열을 **반드시 올려라**(v1→v2 …). 클라와 서버가 이 파일을 공유하므로, 배포
# 시차로 구세대 Honey 가 올린 pack 은 포맷 문자열이 달라 parse_pack_index /
# validate_pack_chunk 에서 거부되고 서버가 폴백 계산한다 — 틀린 분포를 dist_pack_store(영구)
# 에 저장해 전 사용자에게 조용히 서빙하는 것을 막는다. (blob 은 응답 포맷 "ecdf-columnar-v1"
# 이 프런트 계약이라 여기처럼 버전을 못 박으므로, 값 로직 변경 시 pack 버전을 올려 클라
# 재빌드를 유도하고 blob 은 캐시라 삭제 후 서버 재계산으로 자정된다.)
DIST_PACK_FORMAT = "dist-pack-v1"
DIST_PACK_INDEX_FORMAT = "dist-pack-index-v1"
# chunk dict 를 separators=(",",":") 로 직렬화한 선두 — 서버가 전체 파싱 없이 포맷만 검증한다.
# FORMAT 에서 파생 → 리비전을 올릴 때 이 선두를 따로 고칠 필요가 없다(단일 지점).
DIST_PACK_PREFIX = ('{"format":"' + DIST_PACK_FORMAT + '","items":').encode("utf-8")

# chunk 당 항목 수. 프런트 배치 크기(distribution.js DIST_BATCH.SIZE=30)와 맞춰,
# 화면에 보이는 한 배치가 대체로 chunk 1~2개만 읽고 끝나게 한다.
CHUNK_ITEMS = 30

# chunk 1개의 무게는 항목 수가 아니라 **항목 × 소스** 에 비례한다 — 한 항목의 payload 가
# 소스마다 (x, c, c1) 3배열이기 때문이다. 항목 수를 30 으로 고정하면 소스가 24개인 세션은
# chunk 하나가 비압축 수십 MB 가 되고, 그걸 조회 스레드가 통째로 gunzip+json.loads 하며
# GIL 을 잡는다(dist_pack_store.load_chunk_items). 그래서 **셀 수**를 목표로 잡고 항목 수를
# 소스 수에 반비례시킨다. 240 = 30 × 8 이라 소스 8개 이하는 종전과 완전히 동일하다.
_CHUNK_TARGET_CELLS = 240
_CHUNK_ITEMS_MIN = 5


def adaptive_chunk_items(n_sources: int) -> int:
    """소스 수에 맞춘 chunk 당 항목 수 (S≤8 이면 CHUNK_ITEMS 그대로)."""
    n = max(1, int(n_sources))
    return max(_CHUNK_ITEMS_MIN, min(CHUNK_ITEMS, round(_CHUNK_TARGET_CELLS / n)))


def _numeric_array(series):
    """Series → numeric ndarray (길이 보존, 비수치는 NaN).

    ``tabs.distribution.to_numeric_clean`` 의 dtype 지름길과 같은 규칙을 쓴다 —
    두 경로가 다른 변환을 쓰면 값이 어긋난다.
    """
    import pandas as pd

    if getattr(series.dtype, "kind", "") in "if":
        return series.to_numpy()
    return pd.to_numeric(series, errors="coerce").to_numpy()


def _item_source_counts(table, item, bin1_mask):
    """항목×소스 1건의 (x, c, c1). np.unique **1회**로 전체·bin1 count 를 함께 낸다.

    오늘 서버는 전체용/bin1용으로 각각 np.unique 를 돌린다. bin1 값 집합은 전체 값 집합의
    부분집합이므로(같은 컬럼의 유한값에 마스크만 더한 것) 전체 고유값에 정렬해 bin1 count 를
    세면 같은 결과를 정렬 1회로 얻는다.
    """
    from .tabs.common import num

    numeric = _numeric_array(table.data[item])
    finite = np.isfinite(numeric)
    values = numeric[finite]
    if values.size == 0:
        return np.empty(0), np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    unique, inverse = np.unique(values, return_inverse=True)
    counts = np.bincount(inverse, minlength=unique.size).astype(np.int64)

    # bin1 = 양품 & 규격 내 (오늘 build_distribution_compact 의 bin1_only 필터와 동일 순서)
    sel = bin1_mask[finite] if bin1_mask is not None else np.zeros(values.size, dtype=bool)
    if sel.any():
        ilo = num(table.lolim.get(item))
        ihi = num(table.hilim.get(item))
        if ilo is not None:
            sel = sel & (values >= ilo)
        if ihi is not None:
            sel = sel & (values <= ihi)
    counts1 = np.bincount(inverse, weights=sel.astype(np.float64),
                          minlength=unique.size).astype(np.int64)
    return np.round(unique, 6), counts, counts1


def _prepare_tables(tables, selected_items, mode):
    """mode 변형(DUT 분할) + selected_items 필터 + 무데이터 항목 제외.

    ``dist_blob.compute_dist_compact`` 앞단과 동일해야 항목 집합이 어긋나지 않는다.
    tables 의 item_columns 를 in-place 필터하므로 소모성 tables 를 넘길 것.
    """
    from .tabs.common import empty_items
    from .validation import mode_tables, validate_mode

    tables = mode_tables(tables, validate_mode(mode))
    selected = {str(v) for v in (selected_items or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]
    excluded = empty_items(tables)
    return tables, excluded


def build_dist_pack(tables, selected_items, mode, *, chunk_items: int | None = None):
    """(index, chunk 제너레이터) 반환 — chunk 를 하나씩 만들어 넘긴다.

    호출자(Honey)가 chunk 를 받는 즉시 gzip 하고 버리면 전체 항목을 한 dict 로 들고 있지
    않아 peak 메모리가 낮다. chunk 분할 순서는 TSEQ(갤러리 표시 순) — 화면에 보이는
    배치가 인접 chunk 에 모이게 하기 위함이며, 서빙 시 항목 정렬과는 무관하다.

    ``chunk_items=None`` 이면 소스 수에 맞춰 자동 결정한다(``adaptive_chunk_items``).
    서버 조회는 index 의 chunk→항목 매핑만 보므로 chunk 크기는 pack 마다 달라도 되고,
    이미 저장된 30 고정 pack 도 그대로 서빙된다(포맷 리비전 대상 아님).
    """
    from .tabs.common import PASS_BIN, bin_types, json_safe, round_num
    from .tabs.distribution import tseq_sort_key

    tables, excluded = _prepare_tables(tables, selected_items, mode)
    if chunk_items is None:
        chunk_items = adaptive_chunk_items(len(tables))
    ordered = sorted({c for t in tables for c in t.item_columns if c not in excluded},
                     key=tseq_sort_key(tables))
    groups = [ordered[i:i + chunk_items] for i in range(0, len(ordered), chunk_items)]
    index = {
        "format": DIST_PACK_INDEX_FORMAT,
        "chunk_items": int(chunk_items),
        "chunks": [{"id": i, "items": list(g)} for i, g in enumerate(groups)],
    }

    bin1_masks = {}
    for table in tables:
        bin1_masks[id(table)] = np.asarray(
            [b == PASS_BIN for b in bin_types(table)], dtype=bool)

    def _chunks():
        for chunk_id, group in enumerate(groups):
            items = {}
            for item in group:
                sources = {}
                units = ""
                lo = hi = None
                first = True
                for table in tables:
                    if item not in table.item_columns:
                        continue
                    if first:
                        units = json_safe(table.units.get(item)) or ""
                        lo = round_num(table.lolim.get(item))
                        hi = round_num(table.hilim.get(item))
                        first = False
                    x, c, c1 = _item_source_counts(table, item, bin1_masks[id(table)])
                    sources[table.source] = {
                        "x": x.tolist(), "c": c.tolist(), "c1": c1.tolist()}
                if sources:
                    items[item] = {"units": units, "lo": lo, "hi": hi, "sources": sources}
            yield chunk_id, {"format": DIST_PACK_FORMAT, "items": items}

    return index, _chunks()


def build_pack_from_parquet(sources, selected_items, mode, *, stage_cb=None, level: int = 1):
    """parquet bytes 목록 → 전송용 pack ({"index": json str, "chunks": {id: gzip bytes}}).

    Honey 가 서버로 보낼 pack 을 만드는 **단일 진입점**이다 — 업로드(honey_main)와 Excel
    왕복 반영(excel_edit) 두 경로가 같은 코드를 써야 서버가 받는 pack 의 값이 같다.
    서버 loader 가 디코드할 것과 동일한 bytes 를 여기서도 디코드하므로 입력 차이가 없다.

    sources: [{"data": parquet bytes, "name": 표시명, "file_name": 원본파일명}, ...]
    stage_cb(msg): 진행 단계 문자열 보고 (워커 스레드에서 호출 — UI 직접 접근 금지).
    반환 None = 만들 항목이 없음(호출부는 미첨부로 진행, 서버가 폴백 계산).
    chunk gzip 레벨 1 — 클라 CPU 절감(LAN 전송량 증가는 미미).
    """
    from .honeyform import decode_split_honeyform_parquet

    def _stage(msg):
        if stage_cb is not None:
            try:
                stage_cb(msg)
            except Exception:
                pass

    _stage("분포 데이터 준비 중... (디코드)")
    tables = []
    for idx, item in enumerate(sources):
        name = str(item.get("name") or f"source_{idx + 1}")
        tables.append(decode_split_honeyform_parquet(
            item["data"], source=name,
            file_name=str(item.get("file_name") or name), keep_df=False))

    index, chunk_iter = build_dist_pack(tables, selected_items, mode)
    total_chunks = len(index.get("chunks") or ())
    # chunk 를 만드는 즉시 gzip 하고 dict 만 남긴다 — 전체 항목 payload 를 한 번에 들고
    # 있지 않아 peak 메모리가 낮다.
    chunks = {}
    for chunk_id, chunk in chunk_iter:
        _stage(f"분포 데이터 생성 중... ({chunk_id + 1}/{total_chunks})")
        chunks[chunk_id] = gzip_pack_chunk(chunk, level=level)
    if not chunks:
        return None
    return {"index": dumps_pack_index(index), "chunks": chunks}


def gzip_pack_chunk(chunk: dict, *, level: int = 6) -> bytes:
    """chunk dict → JSON gzip bytes (dist_blob 과 동일한 직렬화 규약)."""
    return gzip.compress(
        json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=level)


def dumps_pack_index(index: dict) -> str:
    return json.dumps(index, ensure_ascii=False, separators=(",", ":"))


def validate_pack_chunk(blob: bytes) -> int:
    """chunk gzip 무결성(CRC) + 포맷 선두 검증. 반환 = 비압축 바이트 수, 실패 시 ValueError.

    dist_blob.validate_dist_blob 과 같은 이유로 전체 JSON 파싱은 하지 않는다 —
    값 정합성은 클라가 서버와 같은 코드를 쓰는 것으로 보장한다(검증: 정준 JSON 일치).
    """
    total = 0
    first = b""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as f:
            chunk = f.read(1 << 16)
            first = chunk
            while chunk:
                total += len(chunk)
                chunk = f.read(1 << 20)
    except (OSError, EOFError) as exc:
        raise ValueError(f"invalid gzip dist pack chunk: {exc}") from exc
    if not first.startswith(DIST_PACK_PREFIX):
        raise ValueError("unexpected dist pack chunk prefix (format mismatch)")
    return total


def parse_pack_index(text) -> dict:
    """index JSON 텍스트 → 검증된 dict. 포맷/구조가 어긋나면 ValueError.

    서버가 모르는 포맷(구/신 클라 세대 차)은 여기서 걸러 폐기하고 기존 계산 폴백으로 간다.
    """
    try:
        index = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid dist pack index json: {exc}") from exc
    if not isinstance(index, dict) or index.get("format") != DIST_PACK_INDEX_FORMAT:
        raise ValueError("unexpected dist pack index format")
    chunks = index.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError("dist pack index has no chunks")
    seen = set()
    for entry in chunks:
        if not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
            raise ValueError("malformed dist pack index chunk entry")
        cid = entry.get("id")
        if not isinstance(cid, int) or cid in seen:
            raise ValueError("bad dist pack chunk id")
        seen.add(cid)
    return index


def item_chunk_map(index: dict) -> dict:
    """항목 → chunk id."""
    out = {}
    for entry in index.get("chunks") or ():
        for item in entry.get("items") or ():
            out[str(item)] = int(entry["id"])
    return out


def _ecdf_sources(sources: dict, *, bin1: bool) -> dict:
    out = {}
    for source, cols in (sources or {}).items():
        # x 는 numpy 로 되돌리지 않는다 — 정수 컬럼의 x 는 오늘 서버도 int 를 내므로
        # float64 로 승격하면 정준 JSON 이 5 ↔ 5.0 으로 어긋난다.
        x = list(cols.get("x") or ())
        raw = list((cols.get("c1") if bin1 else cols.get("c")) or ())
        if bin1:
            kept = [i for i, n in enumerate(raw) if n]
            x = [x[i] for i in kept]
            raw = [raw[i] for i in kept]
        counts = np.asarray(raw, dtype=np.int64)
        n = int(counts.sum())
        if n <= 0 or not x:
            out[source] = {"x": [], "y": []}
            continue
        # 오늘 서버와 같은 연산 순서 — cumsum → n 으로 나눔 → *100 → round3.
        cum = np.cumsum(counts) / n * 100.0
        out[source] = {"x": x, "y": np.round(cum, 3).tolist()}
    return out


def ecdf_from_pack_items(pack_items: dict, *, bin1: bool = False, only=None) -> dict:
    """pack 항목 dict → ``ecdf-columnar-v1`` compact dict (정렬 없음, 덧셈만).

    항목 키는 **사전순**으로 낸다 — 서버 폴백(compute_dist_compact)의 항목 순서와 같아야
    정준 JSON 이 일치한다(pack 의 chunk 분할은 TSEQ 순이라 서로 다르다).
    """
    names = sorted(str(k) for k in (pack_items or {}))
    if only is not None:
        wanted = {str(v) for v in only if str(v)}
        names = [n for n in names if n in wanted]
    items = {}
    for name in names:
        entry = pack_items.get(name) or {}
        items[name] = {
            "units": entry.get("units") or "",
            "lo": entry.get("lo"),
            "hi": entry.get("hi"),
            "sources": _ecdf_sources(entry.get("sources") or {}, bin1=bin1),
        }
    return {"format": "ecdf-columnar-v1", "items": items}


def load_chunk_items_sized(blob: bytes) -> tuple[dict, int]:
    """chunk gzip bytes → (items dict, 비압축 JSON 바이트 수).

    크기는 dist_pack_store 의 디코드 캐시가 바이트 상한을 걸 때 쓴다 — 여기서 이미
    비압축 text 를 만들므로 호출부가 재직렬화할 필요가 없다.
    """
    raw = gzip.decompress(blob)
    chunk = json.loads(raw.decode("utf-8"))
    if not isinstance(chunk, dict) or chunk.get("format") != DIST_PACK_FORMAT:
        raise ValueError("unexpected dist pack chunk format")
    return chunk.get("items") or {}, len(raw)


def load_chunk_items(blob: bytes) -> dict:
    """chunk gzip bytes → items dict (서버 조회 경로)."""
    return load_chunk_items_sized(blob)[0]
