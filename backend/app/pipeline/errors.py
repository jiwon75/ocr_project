"""
[이 파일이 하는 일]
파일 전체를 실패로 만드는 오류(PipelineError)와, 사용자에게 보여 줄 실패 문장을 정의한다.

[왜 이렇게 나눴는지]
"ValueError: cannot identify image" 같은 오류 문장은 개발자만 이해할 수 있다.
오류 종류(reason)별로 사용자가 이해할 수 있는 한국어 문장을 한 곳에 모아 두면
프론트 화면, status API, 로그에서 모두 같은 설명을 쓸 수 있다.
"""


class PipelineError(Exception):
    """
    파일 전체 처리를 멈춰야 하는 오류.
    - stage : 어느 단계에서 났는지 (upload/preprocess/layout/process/merge/db)
    - reason: 짧은 오류 코드 (예: "corrupted_file")
    - message: 사용자에게 보여 줄 문장
    """

    def __init__(self, stage: str, reason: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.reason = reason
        self.message = message


# 오류 코드 → 사용자 안내 문장. {값} 자리는 설정값으로 채운다 ([규칙 4] 숫자를 문장에 직접 쓰지 않는다)
USER_MESSAGES = {
    "timeout": "처리 시간이 제한({timeout}초)을 넘어 중단되었습니다. 페이지 수를 줄이거나 더 작은 파일로 다시 시도해 주세요.",
    "corrupted_file": "파일이 손상되었거나 열 수 없는 형식입니다.",
    "too_many_pages": "페이지 수가 너무 많습니다. 최대 {max_pages}페이지까지 처리할 수 있습니다.",
    "encrypted": "암호가 걸린 문서는 처리할 수 없습니다. 암호를 해제한 뒤 다시 올려 주세요.",
    "hwp_distribution": "배포용(복사·인쇄 제한) HWP 문서는 내용을 읽을 수 없습니다.",
    "empty_document": "문서에서 내용을 찾지 못했습니다.",
    "worker_crashed": "처리 중 작업 프로그램이 비정상 종료되었습니다. 잠시 후 다시 시도해 주세요.",
    "worker_unavailable": "OCR 모델을 불러오지 못해 처리할 수 없습니다. 관리자에게 문의해 주세요.",
    "internal_error": "처리 중 예상하지 못한 오류가 발생했습니다.",
}


def user_message(reason: str, **values) -> str:
    """오류 코드에 맞는 안내 문장을 돌려준다. 모르는 코드면 일반 문장."""
    template = USER_MESSAGES.get(reason, USER_MESSAGES["internal_error"])
    try:
        return template.format(**values)
    except KeyError:
        return template
