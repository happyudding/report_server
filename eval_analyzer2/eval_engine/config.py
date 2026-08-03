"""설정 — DB 경로 / LLM 어댑터 / rules 파일 경로. 환경변수 override.

LLM 모델/endpoint 는 **사용자가 지정**한다. 기본 모델 하드코딩 금지(EVAL_LLM_* 비우면 LLM off).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # eval_analyzer/
DATA_DIR = Path(os.environ.get("EVAL_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("EVAL_DB_PATH", DATA_DIR / "eval.db"))
RULES_DIR = Path(os.environ.get("EVAL_RULES_DIR", Path(__file__).resolve().parent / "rules"))

THRESHOLDS_FILE = RULES_DIR / "thresholds.yaml"
SIGNATURES_FILE = RULES_DIR / "signatures.yaml"
BIN_TAXONOMY_FILE = RULES_DIR / "bin_taxonomy.yaml"
ITEM_ALIAS_FILE = RULES_DIR / "item_alias.yaml"
PRODUCT_TAXONOMY_FILE = RULES_DIR / "product_taxonomy.yaml"
OUTCOME_TAXONOMY_FILE = RULES_DIR / "outcome_taxonomy.yaml"

ENGINE_VERSION = os.environ.get("EVAL_ENGINE_VERSION", "ev1")

# ── LLM 어댑터 (provider-agnostic, 사용자 지정) ──────────────────────────────
EVAL_LLM_ENABLED = os.environ.get("EVAL_LLM_ENABLED", "false").lower() == "true"
EVAL_LLM_ENDPOINT = os.environ.get("EVAL_LLM_ENDPOINT", "")   # OpenAI 호환 chat URL 등
EVAL_LLM_MODEL = os.environ.get("EVAL_LLM_MODEL", "")         # 사용자 지정, 기본값 없음
EVAL_LLM_API_KEY = os.environ.get("EVAL_LLM_API_KEY", "")
EVAL_LLM_TIMEOUT = float(os.environ.get("EVAL_LLM_TIMEOUT", "30"))

# 선례(precedent) 매칭 — [req1] (bin + value_type + item명 퍼지)
PRECEDENT_NAME_SIMILARITY = float(os.environ.get("EVAL_PRECEDENT_SIM", "0.70"))

# ── 선례검색 백엔드 (교체형: sql 기본 | rag) ──────────────────────────────
EVAL_PRECEDENT_BACKEND = os.environ.get("EVAL_PRECEDENT_BACKEND", "sql").lower()
EVAL_PRECEDENT_RAG_ENDPOINT = os.environ.get("EVAL_PRECEDENT_RAG_ENDPOINT", "")
EVAL_PRECEDENT_RAG_TOPK = int(os.environ.get("EVAL_PRECEDENT_RAG_TOPK", "5"))
# min-n 가드
N_MIN_HIGH_MOMENT = int(os.environ.get("EVAL_N_MIN", "20"))
