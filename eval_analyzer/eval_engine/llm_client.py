"""provider-agnostic LLM 어댑터. 모델/endpoint 는 사용자 지정(config.EVAL_LLM_*).

기본 모델 하드코딩 금지. EVAL_LLM_ENABLED=False 면 호출 안 함(상위에서 템플릿 fallback).
기본 shape 은 OpenAI 호환 chat completions(POST endpoint, messages). 다른 provider 면 여기만 교체.
"""
from . import config


def is_enabled() -> bool:
    return config.EVAL_LLM_ENABLED and bool(config.EVAL_LLM_ENDPOINT) and bool(config.EVAL_LLM_MODEL)


def complete(prompt: str, *, model_version: str | None = None) -> str:
    """프롬프트 → 코멘트 텍스트. 실패 시 예외(상위에서 fallback).

    TODO: urllib/requests 로 config.EVAL_LLM_ENDPOINT 에 POST.
    payload(OpenAI 호환): {"model": model_version or config.EVAL_LLM_MODEL,
                           "messages":[{"role":"user","content":prompt}]}
    헤더: Authorization: Bearer config.EVAL_LLM_API_KEY. timeout=config.EVAL_LLM_TIMEOUT.
    """
    raise NotImplementedError("사용자 지정 모델 endpoint 로 HTTP 호출 구현")
