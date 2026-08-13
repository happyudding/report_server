"""Honey 클라 Excel 편집용 raw data export / replace.

세션의 web_report parquet 원본을 zip 으로 내보내고(Honey 가 Excel 로 열어 편집),
Honey 가 재인코딩한 parquet 전체를 받아 기존 analysis_key 원본을 통째로 덮어쓴다.
덮어쓰기 직전 현재 원본을 1세대 백업한다(backup_current_sources) — 앱 내 undo 는 없고
복구는 운영자 수동. service.edit_raw_data 와 동일한 백업·content_hash 산출·캐시 무효화·
audit 패턴을 그대로 따른다 (여기는 셀 단위가 아니라 source 전체 교체라는 점만 다름).

웹 브라우저용 CSV 내보내기(export_source_csv = source 1개, export_sources_csv_zip =
전 source 를 zip 하나로)도 여기 둔다 — Honey 없이 세션 rawdata 원본을 받는 조회 전용 경로다.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import cache
from . import edits
from . import runtime
from .honeyform import META_COLUMNS, validate_parquet_bytes
from .validation import canon

_log = logging.getLogger(__name__)


def _save_dist_pack(dist_pack, analysis_key, content_hash, mode, upload_root) -> bool:
    """클라 첨부 Distribution pack 저장 (업로드 경로와 같은 구현).

    실패는 무해 — 서버가 조회 때 폴백 계산한다(느릴 뿐 결과는 같다)."""
    if not dist_pack:
        return False
    try:
        from .ingest import save_client_dist_pack
        from .validation import validate_mode

        return save_client_dist_pack(dist_pack, analysis_key, content_hash,
                                     validate_mode(mode), Path(upload_root))
    except Exception:
        _log.warning("rawdata_replace dist pack 저장 실패 akey=%.12s",
                     str(analysis_key), exc_info=True)
        return False


def export_etag(session) -> str:
    """rawdata_export 응답의 ETag — 원본 parquet 내용 해시(content_hash) 그대로.

    Honey 가 temp 에 받아둔 zip 을 재사용할 수 있는지 판정하는 유일한 기준이다:
    content_hash 는 raw parquet 이 바뀔 때만(셀 편집·Excel 왕복) 바뀌므로, 같으면
    내려줄 내용이 100% 동일하다. 값이 없는 legacy 세션은 빈 문자열(=캐시 불가).
    """
    return str((session or {}).get("content_hash") or "")


def export_sources_zip(session_id, *, report_db, upload_root) -> bytes:
    """세션의 모든 source parquet + manifest 를 zip(ZIP_STORED) bytes 로 반환.

    parquet 은 이미 zstd 압축이라 재압축하지 않는다. 없으면 KeyError/FileNotFoundError.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    sources, manifest = runtime.storage().load_webreport_sources(analysis_key, upload_root)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for idx, data in enumerate(sources):
            zf.writestr(f"source_{idx}.parquet", data)
    return buf.getvalue()


def _csv_row(values) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(values)
    return buf.getvalue()


# 파일명에 쓸 수 없는 문자 — 단일 CSV 다운로드와 zip 내부 이름이 같은 규칙을 쓰게 여기 둔다.
_FS_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n]+')


def csv_download_name(lot, source_name) -> str:
    """rawdata CSV 파일명 — 단일 다운로드 파일명이자 전체 zip 의 내부 파일명.

    전체 zip 을 푼 뒤 개별 source 를 추가로 받아도 같은 폴더에서 이름이 겹치지 않고
    일관되도록 두 경로가 이 함수 하나를 공유한다. `/`·`\\` 가 `_` 로 치환되므로 zip
    내부 이름에 경로 구분자가 섞일 여지(zip slip)도 함께 사라진다.
    """
    return _FS_UNSAFE.sub("_", f"rawdata_{lot}_{source_name}.csv")


def _load_session_sources(session_id, report_db, upload_root):
    """세션 → (session, source parquet bytes 리스트, manifest).

    없는 세션은 KeyError, 산출물이 없는 세션은 FileNotFoundError.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    sources, manifest = runtime.storage().load_webreport_sources(analysis_key, upload_root)
    return session, sources, manifest


def _open_source(sources, manifest, idx):
    """source idx 의 parquet 을 열어 (pf, names, batch_rows, source_name) 을 준다.

    footer 만 읽는다 — 행 데이터는 iter_batches 때 들어온다. BufferReader 는 io.BytesIO
    와 달리 원본 bytes 를 복사하지 않는다(zero-copy) — 전 source 를 동시에 여는
    export_sources_csv_zip 에서 peak 메모리가 2배가 되는 것을 막는다.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not 0 <= idx < len(sources):
        raise IndexError(idx)
    entries = manifest.get("sources") or []
    entry = entries[idx] if idx < len(entries) else {}
    # loader.download_decode_tables 와 같은 이름 규칙 — 화면 source 이름과 일치시킨다.
    source_name = str(entry.get("name") or "").strip() or f"source_{idx + 1}"

    pf = pq.ParquetFile(pa.BufferReader(pa.py_buffer(sources[idx])))
    names = [str(c) for c in pf.schema_arrow.names]
    if len(names) >= len(META_COLUMNS):
        # 조회 경로(_decode_parts)와 같은 구제 — legacy 'Bin' 헤더도 규격대로 내보낸다.
        names[:len(META_COLUMNS)] = META_COLUMNS
    # 청크는 셀 수로 잡는다 — 행 수로 고정하면 컬럼이 넓은 소스에서 청크 하나가 수십 MB 가 된다.
    batch_rows = max(50, min(1000, 200_000 // max(1, len(names))))
    return pf, names, batch_rows, source_name


def _csv_chunks(pf, names, batch_rows):
    """헤더 1행 + batch 별 CSV 텍스트 청크 (str generator). BOM 은 붙이지 않는다."""
    yield _csv_row(names)
    for batch in pf.iter_batches(batch_size=batch_rows):
        yield batch.to_pandas().to_csv(header=False, index=False, lineterminator="\r\n")


def export_source_csv(session_id, source_idx, *, report_db, upload_root):
    """세션의 source 1개를 7-meta honeyform 원형 그대로 CSV 청크로 흘려보낸다.

    반환: (chunks, source_name) — chunks 는 문자열 generator(첫 청크에 UTF-8 BOM).
    없는 세션/산출물은 KeyError/FileNotFoundError, source_idx 범위 밖은 IndexError.

    parquet 은 전 셀 string 으로 저장돼 있으므로(honeyform._string_frame_for_parquet)
    숫자 재포맷 없이 저장된 문자 그대로가 나간다. 메타 6행(TSEQ~LOLIM)도 자르지 않는다 —
    파일 하나가 곧 업로드 당시의 원본이다. 전처리·편집 상태는 반영하지 않는다.

    응답을 흘리기 시작한 뒤에는 4xx/5xx 로 돌아갈 수 없으므로, 파일을 열어 스키마를
    읽는 데까지는 generator 밖(=라우트가 아직 상태 코드를 정할 수 있는 시점)에서 끝낸다.
    행 데이터는 batch 단위로 읽어 넘긴다 — 컬럼 2000개짜리 소스를 통째로 pandas 프레임
    으로 펼치면 CSV 텍스트와 프레임을 동시에 들고 있게 된다.
    """
    _session, sources, manifest = _load_session_sources(session_id, report_db, upload_root)
    pf, names, batch_rows, source_name = _open_source(sources, manifest, source_idx)

    def chunks():
        inner = _csv_chunks(pf, names, batch_rows)
        # 선두 U+FEFF(BOM) — Excel 이 CSV 를 UTF-8 로 열게 하는 유일한 신호. 눈에 안 보이는
        # 문자를 소스에 직접 박으면 지워져도 티가 안 나므로 코드포인트로 쓴다.
        yield chr(0xFEFF) + next(inner)      # 첫 청크(헤더)에만 BOM
        yield from inner

    return chunks(), source_name


class _ZipSink:
    """zipfile 이 쓴 바이트를 모았다가 generator 가 뽑아가는 unseekable sink.

    seek·tell 을 **일부러 제공하지 않는다** — 있으면 zipfile 이 엔트리를 다 쓴 뒤 로컬
    헤더로 되감아 CRC/크기를 고치려 들어(_ZipWriteFile.close) 스트리밍이 불가능해진다.
    없으면 zipfile 이 _Tellable 로 감싸고 _seekable=False 로 내려가 CRC/크기를 데이터
    뒤 data descriptor 로 붙인다 — 표준 압축 해제 도구가 정상 인식하는 방식이다.

    write 는 **쓴 바이트 수를 반환해야** 하고(_Tellable.write 가 offset 에 더한다),
    flush 는 **있어야 한다**(_write_end_record 가 fp.flush() 를 부른다). 둘 중 하나만
    빠져도 zip 마감에서 터진다.
    """
    __slots__ = ("_parts",)

    def __init__(self):
        self._parts = []

    def write(self, data):
        self._parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def drain(self) -> bytes:
        parts, self._parts = self._parts, []
        return b"".join(parts)


def _unique_entry_name(name, used) -> str:
    """zip 내부 이름 중복 제거 — 같은 이름이 또 오면 stem 에 _2, _3 을 붙인다.

    zipfile 은 중복 이름을 UserWarning 만 내고 그대로 써서, 푸는 쪽에서 하나가 다른
    하나를 조용히 덮어쓴다(= source 유실). 키는 소문자로 비교한다 — Windows 파일
    시스템이 대소문자를 구분하지 않으므로 'A.csv'/'a.csv' 도 충돌이다.
    """
    key = name.lower()
    if key not in used:
        used[key] = 1
        return name
    stem, _, ext = name.rpartition(".")
    while True:
        used[key] += 1
        cand = f"{stem}_{used[key]}.{ext}"
        if cand.lower() not in used:
            used[cand.lower()] = 1
            return cand


def export_sources_csv_zip(session_id, *, report_db, upload_root):
    """세션의 전 source 를 CSV 로 만들어 **스트리밍 zip** bytes 청크로 흘려보낸다.

    반환: (chunks, count) — chunks 는 bytes generator, count 는 담기는 source 개수.
    없는 세션/산출물은 KeyError/FileNotFoundError, source 가 0개면 IndexError.

    내용 정책은 export_source_csv 와 완전히 같다(저장된 parquet 원형, 메타 6행 포함,
    전처리·편집 미반영). 파일마다 UTF-8 BOM 을 넣는다 — 풀어서 Excel 로 바로 연다.
    zip 내부 파일명도 단일 다운로드와 같은 csv_download_name 규칙을 쓴다.

    압축은 ZIP_DEFLATED(level 1) — parquet 을 담는 export_sources_zip 이 ZIP_STORED 인
    것과 다르다. 저쪽은 이미 zstd 압축이라 재압축이 CPU 만 먹지만 여기는 순수 텍스트라
    실측 1.6배(level 1)~2.6배(level 6)가 줄어든다. level 을 1 로 고정한 이유는 사내
    LAN 에서 병목이 대역폭이 아니라 waitress 스레드 점유 시간이기 때문이다 — 20MB 당
    level 1 은 0.09s(234MB/s), level 6 은 0.44s(48MB/s) 를 쓰고 절약분은 원본의 4% 다.

    zip 전체를 메모리에 만들어 두지 않는다. CSV 는 parquet 대비 전개 크기가 몇 배라
    전량을 들고 있으면 대형 세션 1건이 웹 프로세스 RAM 을 통째로 먹는다.
    """
    _session, sources, manifest = _load_session_sources(session_id, report_db, upload_root)
    if not sources:
        raise IndexError(0)
    lot = str((_session or {}).get("lot_id") or "").strip() or str(session_id)

    # 응답을 흘리기 시작하면 4xx/5xx 로 돌아갈 수 없으므로, 실패할 수 있는 일(스키마 읽기·
    # 이름 확정)은 전부 여기서 끝낸다 — export_source_csv 와 같은 판단. footer 만 읽으므로
    # 전 source 를 미리 열어도 행 데이터는 아직 안 들어온다.
    opened, used_names = [], {}
    for idx in range(len(sources)):
        pf, names, batch_rows, source_name = _open_source(sources, manifest, idx)
        entry_name = _unique_entry_name(csv_download_name(lot, source_name), used_names)
        opened.append((pf, names, batch_rows, entry_name))
    sources.clear()      # 원본 bytes 는 BufferReader 가 참조로 들고 있다(zero-copy)

    def chunks():
        sink = _ZipSink()
        # with 를 쓰지 않는다 — __exit__ 가 예외 상황에서도 close() 로 중앙 디렉토리를 써서
        # "정상적으로 열리지만 source 가 빠진 zip" 을 만든다(조용한 유실). 성공했을 때만 닫는다.
        zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
        try:
            for i, item in enumerate(opened):
                pf, names, batch_rows, entry_name = item
                # 단일 엔트리 4GB 초과 시 zip64 가 필요하다(force_zip64=True). 현 규모
                # (source 당 parquet 수 MB)에서는 도달하지 않으므로 켜지 않는다.
                with zf.open(entry_name, "w") as w:
                    inner = _csv_chunks(pf, names, batch_rows)
                    w.write((chr(0xFEFF) + next(inner)).encode("utf-8"))
                    for text in inner:
                        w.write(text.encode("utf-8"))
                        out = sink.drain()
                        if out:
                            yield out
                out = sink.drain()      # 엔트리 마감(deflate flush + data descriptor)
                if out:
                    yield out
                opened[i] = None        # 끝난 source 의 parquet bytes 를 즉시 회수
            zf.close()                  # ← 성공 경로에서만 중앙 디렉토리를 쓴다
            out = sink.drain()
            if out:
                yield out
        except GeneratorExit:
            zf.fp = None                # 클라 중단 — 마감 없이 종료
            raise
        except Exception:
            # 이미 200 을 보낸 뒤라 상태 코드를 바꿀 수 없다. 중앙 디렉토리 없이 끊어
            # 받는 쪽이 "손상된 zip"(BadZipFile)으로 인지하게 한다.
            # zf.fp=None 은 GC __del__ → close() 재진입을 막는다(close 는 fp None 이면 즉시 반환).
            zf.fp = None
            _log.exception("rawdata csv zip 스트리밍 중단 (session=%s)", session_id)
            return

    return chunks(), len(opened)


def backup_current_sources(analysis_key, upload_root, old_content_hash="") -> str:
    """덮어쓰기 직전 현재 parquet 원본+manifest 를 로컬 백업으로 남긴다 (1세대 유지).

    `<upload_root>/webreport_backup/<analysis_key>/<UTC ts>_<old_hash 12자>/` 에
    source_<idx>.parquet + manifest.json 을 쓰고, 성공 후 같은 analysis_key 의 이전
    세대 디렉토리를 지워 항상 1세대만 유지한다(akey당 자체 용량 상한). 실패는 예외
    전파 — 백업 없이 덮어쓰지 않는다(호출부가 편집을 거부, 원본 무손상). 복구는
    수동(파일 복사) 전제라 복원 API 는 없다. 반환은 백업 디렉토리 이름(감사 로그용).
    """
    sources, manifest = runtime.storage().load_webreport_sources(analysis_key, upload_root)
    root = Path(upload_root) / "webreport_backup" / str(analysis_key)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{stamp}_{str(old_content_hash or '')[:12] or 'nohash'}"
    dest = root / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for idx, data in enumerate(sources):
        (dest / f"source_{idx}.parquet").write_bytes(data)
    # 백업 완료 후에만 이전 세대 제거 — 도중 실패 시 남는 부분 디렉토리는 다음
    # 성공 백업이 지운다(덮어쓰기 자체가 거부되므로 원본 유실과 무관).
    for sibling in root.iterdir():
        if sibling.name != name and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)
    return name


def remove_backups(analysis_key, upload_root) -> bool:
    """`webreport_backup/<analysis_key>/` 백업 디렉토리 제거 — 마지막 참조 세션 삭제 시.

    산출물(parquet/이미지)은 storage_gateway 가 지우지만 이 백업은 여기서 만들므로 삭제도
    여기서 제공한다. 있었으면 True. best-effort(rmtree ignore_errors) — 삭제 실패가 세션
    삭제를 막지 않는다.
    """
    root = Path(upload_root) / "webreport_backup" / str(analysis_key)
    if not root.is_dir():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


def _validate_kept_indices(kept_indices, existing, uploaded):
    """kept_indices(남긴 source 의 원본 idx) 검증 — 위반은 전부 ValueError → 400.

    오름차순·중복 없음·범위 내를 강제하고, 업로드 개수와 길이가 일치해야 한다
    (kept_indices[i] 가 webreport_i 의 원본 idx). 개수가 늘어나는 요청(시트 추가)은 거부한다.
    전체 유지(= range(existing))는 통과시킨다 — 클라는 이 경우 필드를 보내지 않지만 받아도
    삭제 없는 교체와 결과가 같다.
    """
    if not kept_indices:
        raise ValueError("source_indices 가 비어 있습니다 — source 를 전부 지울 수 없습니다.")
    if len(kept_indices) != uploaded:
        raise ValueError(
            f"source_indices 개수 불일치: 업로드 {uploaded}, indices {len(kept_indices)}")
    if len(kept_indices) > existing:
        raise ValueError(
            f"source 추가 불가: 기존 {existing}, 요청 {len(kept_indices)}")
    if len(set(kept_indices)) != len(kept_indices):
        raise ValueError("source_indices 에 중복이 있습니다.")
    if list(kept_indices) != sorted(kept_indices):
        raise ValueError("source_indices 는 오름차순이어야 합니다.")
    if any(not 0 <= i < existing for i in kept_indices):
        raise ValueError(
            f"source_indices 범위 밖: 기존 source {existing}개, 요청 {list(kept_indices)}")


def replace_sources(session_id, *, report_db, upload_root, sources_bytes,
                    kept_indices=None, client_ip: str = "", user_agent: str = "",
                    client_user: str = "", dist_pack=None) -> dict:
    """Honey 가 Excel 편집 후 재인코딩한 parquet 전체로 세션 원본을 덮어쓴다.

    kept_indices: 남긴 source 의 원본 idx 리스트(오름차순). None 이면 전체 교체 —
    개수가 기존과 일치해야 한다(구클라 하위호환). 개수가 줄어드는 요청(Excel 시트 삭제)은
    kept_indices 가 필수이며, 빠진 source 를 물리 제거하고 manifest 의 sources 목록도
    함께 축소해 재저장한다 (manifest 불변 스냅샷 규칙의 유일한 예외 — 남기면 idx 와
    parquet 대응이 어긋난다). 삭제 전 원본은 backup_current_sources 로 1세대 백업된다.

    그 밖의 검증은 각 parquet 가 유효한 honeyform 인지 뿐 — 통과하면 무조건 덮어쓴다.

    dist_pack: Honey 가 재인코딩한 parquet 으로 미리 만든 Distribution pack
    ({"index": json str, "chunks": {id: gzip bytes}}). 있으면 새 content_hash 로 영구
    저장해 서버의 콜드 dist 정렬(수십 초 CPU)을 없앤다 — 업로드 경로와 같은 구조다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # (1) 각 parquet 가 유효한 honeyform 인지 검증 (실패 시 ValueError → 400).
    # 뼈대(스키마+메타 6행)만 읽는다 — 클라가 encode 시 이미 같은 규칙으로 검증했고,
    # 여기서 전량 디코드하면 수백만 셀 to_numeric 을 하고 결과를 버리게 된다.
    for i, data in enumerate(sources_bytes):
        try:
            validate_parquet_bytes(data)
        except ValueError as exc:
            raise ValueError(f"source_{i}: {exc}") from exc

    # 같은 analysis_key 원본의 read-modify-write 직렬화 — service.edit_raw_data 와 같은
    # 락 키로 동시 편집 lost update 방지 (단일 프로세스 전제, in-process 락).
    with cache.keyed_lock_ctx(("rawedit", analysis_key)):
        existing = sum(
            1 for o in report_db.get_all_object_infos(analysis_key)
            if str(o.get("object_type", "")).startswith("web_report_source_")
        )
        manifest = runtime.storage().load_webreport_manifest(analysis_key, upload_root)
        old_sources = list(manifest.get("sources") or [])
        if not existing:
            # legacy 무기록 세션 — object_info 대신 manifest 로 기존 개수를 잡는다
            existing = len(old_sources)

        # (2) source 개수 검사 / kept_indices 검증
        removed_names = []
        if kept_indices is None:
            if existing and len(sources_bytes) != existing:
                raise ValueError(
                    f"source 개수 불일치: 기존 {existing}, 업로드 {len(sources_bytes)}")
        else:
            _validate_kept_indices(kept_indices, existing, len(sources_bytes))
            if len(kept_indices) < existing:
                keep = set(kept_indices)
                removed_names = [
                    str((old_sources[i] if i < len(old_sources) else {}).get("name")
                        or f"source_{i}")
                    for i in range(existing) if i not in keep]
                manifest = dict(manifest)
                manifest["sources"] = [old_sources[i] for i in kept_indices
                                       if i < len(old_sources)]

        content_hash = hashlib.sha256(
            canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
        ).hexdigest()

        # 덮어쓰기 직전 현재 원본 1세대 백업 — 실패 시 예외로 편집 거부(원본 무손상).
        backup_name = backup_current_sources(
            analysis_key, upload_root,
            old_content_hash=session.get("content_hash") or "")

        storage_result = runtime.storage().save_webreport_sources(
            analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

        # dedup 형제 세션까지 갱신 — 물리 원본이 바뀌었으므로 같은 analysis_key 를 쓰는
        # 다른 세션이 옛 hash 로 stale disk_cache payload 를 서빙하면 안 된다.
        report_db.update_content_hash_for_analysis_key(analysis_key, content_hash)
        # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
        # (edit_raw_data 와 동일한 무효화 로직).
        cache.evict_akey_caches(analysis_key)
        # 구 세대 Distribution pack 회수 — 새 chash 로는 조회되지 않지만(디렉토리명에 chash)
        # 남겨두면 용량만 먹는다. 이후 조회는 pack 없이 기존 계산 폴백으로 동작한다.
        from . import dist_pack_store

        dist_pack_store.delete_stale(Path(upload_root), analysis_key, content_hash)
        # 클라가 새 parquet 으로 만들어 보낸 pack 을 새 chash 로 저장 — 있으면 이후 조회가
        # 정렬 없이 덧셈만 한다(업로드 직후와 같은 상태). delete_stale 뒤에 저장할 것.
        pack_saved = _save_dist_pack(dist_pack, analysis_key, content_hash,
                                     session.get("mode"), upload_root)
        # 행 위치 기반 전처리 셀 패치는 원본이 바뀌면 엉뚱한 행을 가리킨다 — 형제까지 해제
        # (service.edit_raw_data 와 같은 헬퍼 · 같은 판단).
        dropped = edits.drop_preprocess_edits_for_akey(report_db, analysis_key, user_agent)
    try:
        removed_note = f", removed={removed_names}" if removed_names else ""
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=(f"raw_data(excel, {len(sources_bytes)} sources"
                            f"{removed_note}, backup={backup_name}"
                            + (f", quick_edits_cleared={dropped}" if dropped else "") + ")"),
            client_ip=client_ip, user_agent=user_agent,
            client_user=client_user or None)
    except Exception:
        pass

    # 캐시를 통째로 비웠으므로 다음 조회자가 콜드 리빌드 전부를 부담한다 — 업로드 경로와
    # 같이 프리웜을 걸어 그 계산을 컴퓨트 워커로 넘긴다(요청 스레드 GIL 비점유, 즉시 반환).
    try:
        from . import compute
        compute.prewarm(session_id, str(upload_root))
    except Exception:
        _log.warning("rawdata_replace 프리웜 시작 실패 (session=%s)", session_id,
                     exc_info=True)

    return {"ok": True, "sources": len(sources_bytes), "removed": len(removed_names),
            "storage": storage_result["storage"], "dist_pack_saved": pack_saved}
