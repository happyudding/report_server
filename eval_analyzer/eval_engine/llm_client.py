"""provider-agnostic LLM 어댑터. 모델/endpoint 는 사용자 지정(config.EVAL_LLM_*).

기본 모델 하드코딩 금지. EVAL_LLM_ENABLED=False 면 호출 안 함(상위에서 템플릿 fallback).
기본 shape 은 OpenAI 호환 chat completions(POST endpoint, messages). 다른 provider 면 여기만 교체.

**이 파일이 엔진의 유일한 LLM 출구다.** 소비자는 pipeline/recommend.py:make_comment 의
[점검제안] 섹션 하나뿐이고, 그쪽은 예외를 잡아 룰 기반 문구로 폴백하므로 여기서는
실패를 숨기지 말고 그대로 올린다(조용한 무응답보다 폴백 사실이 드러나는 편이 낫다).

의존은 표준 라이브러리뿐이다(urllib). 운영 venv 가 Python 3.14 라 무거운 SDK 를 새로
얹는 위험이 이득보다 크고, 필요한 것은 JSON POST 한 번뿐이다.
"""
import json
import urllib.request

from . import config


def is_enabled() -> bool:
    """LLM 을 실제로 부를 수 있는 상태인가 — 플래그 + endpoint + model 이 모두 있어야 True.

    셋 중 하나라도 비면 False 라, 설정을 반만 해 둔 채 NotImplementedError 로 터지는 일이 없다.
    """
    return config.EVAL_LLM_ENABLED and bool(config.EVAL_LLM_ENDPOINT) and bool(config.EVAL_LLM_MODEL)


def chat_url(endpoint: str | None = None) -> str:
    """EVAL_LLM_ENDPOINT → 실제로 POST 할 chat completions URL.

    사내 배포마다 주는 값이 다르다: OpenAI SDK 관례대로 base URL(`http://host:8000/v1`)을
    주기도 하고, 완성된 경로(`.../v1/chat/completions`)를 주기도 한다. 둘 다 받아 준다 —
    이 차이 하나로 배선이 404 로 조용히 실패하던 자리다.
    그 외 경로(자체 게이트웨이 등)는 **사용자가 준 그대로** 쓴다(임의로 덧붙이지 않는다).
    """
    url = str(endpoint if endpoint is not None else config.EVAL_LLM_ENDPOINT or "").strip()
    url = url.rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url


def complete(prompt: str, *, model_version: str | None = None) -> str:
    """프롬프트 → 코멘트 텍스트. 실패 시 예외(상위에서 fallback).

    payload(OpenAI 호환): {"model": …, "messages":[{"role":"user","content":prompt}]}
    헤더: Authorization: Bearer config.EVAL_LLM_API_KEY (키가 있을 때만).
    timeout=config.EVAL_LLM_TIMEOUT.
    """
    url = chat_url()
    if not url:
        raise ValueError("EVAL_LLM_ENDPOINT 미설정")
    payload = {
        "model": model_version or config.EVAL_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if config.EVAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.EVAL_LLM_API_KEY}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=config.EVAL_LLM_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    # 응답 shape 이 다르면 KeyError/IndexError 가 그대로 올라간다 — 상위 폴백이 받는다.
    return body["choices"][0]["message"]["content"]
