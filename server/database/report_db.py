"""report DB facade — 구현은 database/ 하위 모듈로 분리 (2026-07-11 Phase 4).

기존 호출부(`from database import report_db` 후 `report_db.xxx(...)`)는 전부
그대로 동작한다. 모듈 구성:

- core.py            스키마(SCHEMA)·마이그레이션·init_report_db·get_conn·analysis lock
- sessions.py        report_session CRUD + 히스토리 + retention 조회
- objects.py         summary 행 / object_info(S3 참조) / csv_files / sheet_data
- audit.py           감사 로그 기록·조회·롤오프
- users.py           즐겨찾기 / 편집 권한 위임 / 방문자 / 개인 중요표시 / (폐지) 로그인 계정
- usage.py           접속 사용량 일별 집계 (Honey 실행 · 웹페이지 방문)
- annotations.py     주석 + Dash 대시보드 편집 셀
- webreport_edits.py web_report 편집 상태 (세션 단위)

새 코드는 세부 모듈을 직접 import 해도 된다. 새 테이블/함수를 추가할 때는
해당 책임 모듈에 넣고 여기 재노출 목록에도 추가할 것.
"""
from .core import (  # noqa: F401
    SCHEMA,
    get_conn,
    init_report_db,
    release_analysis_lock,
    try_acquire_analysis_lock,
)
from .models import Session  # noqa: F401  (get_session 반환 타입 — Mapping 호환)
from .sessions import (  # noqa: F401
    count_by_product_type,
    count_history,
    count_sessions_for_analysis_key,
    create_session,
    delete_analysis_rows,
    delete_session,
    get_expired_sessions,
    get_history,
    get_history_page,
    get_orphan_pending_sessions,
    get_session,
    get_session_by_dataset_id,
    get_session_path_by_analysis_key,
    get_trashed_sessions,
    restore_session,
    session_ids_for_analysis_key,
    trash_session,
    update_content_hash_for_analysis_key,
    update_session,
    update_session_meta,
)
from .objects import (  # noqa: F401
    delete_object_info,
    get_all_object_infos,
    get_all_sheet_data,
    get_csv_files,
    get_object_info,
    get_sheet_data,
    get_summary_by_analysis_key,
    has_summary,
    save_summary_batch,
    touch_object_info,
    upsert_csv_file,
    upsert_object_info,
    upsert_sheet_data,
)
from .audit import (  # noqa: F401
    get_audit_logs,
    log_audit,
    purge_audit_logs,
    recent_upload_user_by_ip,
)
from .users import (  # noqa: F401
    add_session_editor,
    create_user,
    get_user,
    get_user_favorites,
    has_honey_history,
    is_session_editor,
    is_user_important,
    list_session_editors,
    record_web_visitor,
    remove_session_editor,
    search_web_visitors,
    set_user_favorite,
    set_user_important,
    update_user_password,
)
from .usage import record_usage, usage_totals  # noqa: F401
from .annotations import (  # noqa: F401
    create_annotation,
    delete_annotation,
    get_annotation,
    get_annotations,
    get_dashboard_comments,
    replace_dashboard_comments,
    update_annotation,
)
from .webreport_edits import (  # noqa: F401
    apply_webreport_edits,
    get_webreport_edit_meta,
    get_webreport_edit_rev,
    get_webreport_edits,
    note_base_token,
    save_note_sheet_checked,
)
