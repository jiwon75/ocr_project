"""
[이 파일이 하는 일]
Ollama(내 PC 에서 LLM 을 돌려 주는 프로그램)의 HTTP API 를 부른다.
  GET  {OLLAMA_URL}/api/tags   설치된 모델 목록 (켜져 있는지 확인용)
  POST {OLLAMA_URL}/api/chat   질문 → 답

[왜 이렇게 나눴는지]
- Ollama 는 "있으면 쓰는" 선택 기능이다. 연결 실패·시간 초과는 모두 LLMError 하나로 바꿔서,
  부르는 쪽은 그 오류 문장을 화면에 보여 주기만 하면 된다. OCR 처리는 이 파일을 전혀 쓰지 않는다.
- 추가 라이브러리 없이 파이썬 기본 urllib 만 쓴다 (설치할 것이 늘지 않게).
"""
import json
import threading
import urllib.error
import urllib.request

# GPU 가 하나라 동시에 여러 요청을 보내면 서로 느려지기만 한다. 한 번에 하나씩 보낸다
_lock = threading.Lock()

# 켜져 있는지 확인하는 요청은 화면이 기다리므로 짧게 끊는다 (모델 답변 시간과는 상관없는 값)
STATUS_TIMEOUT_SEC = 3


class LLMError(RuntimeError):
    """Ollama 를 쓸 수 없을 때. message 는 사용자에게 그대로 보여 줄 한국어 문장."""


def _request(url: str, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def status(settings) -> dict:
    """
    LLM 도우미를 쓸 수 있는지 알려 준다. 오류를 던지지 않는다.
    돌려주는 값: {"enabled", "reachable", "model", "model_available", "models", "message"}
    """
    info = {"enabled": settings.LLM_ENABLED, "reachable": False, "model": settings.OLLAMA_MODEL,
            "model_available": False, "models": [], "message": None}
    if not settings.LLM_ENABLED:
        info["message"] = "LLM 도우미가 꺼져 있습니다 (.env 의 LLM_ENABLED)."
        return info
    try:
        tags = _request(settings.OLLAMA_URL.rstrip("/") + "/api/tags", None, STATUS_TIMEOUT_SEC)
    except (urllib.error.URLError, OSError, ValueError):
        info["message"] = f"Ollama 에 연결할 수 없습니다 ({settings.OLLAMA_URL}). Ollama 를 켠 뒤 다시 시도해 주세요."
        return info
    info["reachable"] = True
    info["models"] = [m.get("name") for m in tags.get("models", [])]
    wanted = settings.OLLAMA_MODEL
    # "gemma4" 처럼 태그 없이 적으면 Ollama 는 "gemma4:latest" 로 본다
    info["model_available"] = any(name in (wanted, f"{wanted}:latest") for name in info["models"])
    if not info["model_available"]:
        info["message"] = f"모델 {wanted} 이(가) 설치되어 있지 않습니다. 터미널에서 `ollama pull {wanted}` 를 실행해 주세요."
    return info


def chat(settings, system: str, user: str, json_mode: bool = False) -> str:
    """
    LLM 에 한 번 묻고 답 글자를 돌려준다.
    json_mode=True 면 Ollama 가 JSON 형식으로만 답하게 강제한다 (교정 제안처럼 코드가 읽을 답).
    temperature=0: 같은 질문에 늘 같은(가장 그럴듯한) 답을 하게 해서, 엉뚱한 답이 나올 가능성을 줄인다.
    think=False: 생각 과정을 길게 쓰는 모델(gemma4, qwen 등)이 답만 바로 하게 한다 (훨씬 빠르다).
    """
    body = {
        "model": settings.OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    if json_mode:
        body["format"] = "json"
    url = settings.OLLAMA_URL.rstrip("/") + "/api/chat"
    with _lock:
        try:
            res = _request(url, body, settings.OLLAMA_TIMEOUT_SEC)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise LLMError(f"Ollama 가 오류를 돌려주었습니다 ({e.code}): {detail}") from e
        except TimeoutError as e:
            raise LLMError(f"Ollama 응답이 {settings.OLLAMA_TIMEOUT_SEC}초 안에 오지 않았습니다.") from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"Ollama 에 연결할 수 없습니다 ({settings.OLLAMA_URL}).") from e
    return (res.get("message") or {}).get("content") or ""
