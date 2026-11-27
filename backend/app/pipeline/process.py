"""
[2단계 — 이 파일이 하는 일]
structure.json 의 블록을 order 순서대로 하나씩 처리기(processors/)에 넘기고,
결과를 results/{block_id}.json 으로 저장한 뒤 structure 의 status 를 done/failed 로 바꾼다.

[왜 이렇게 나눴는지]
- "블록 하나 처리"(process_block)를 따로 함수로 만들어 두면, 전체 처리와 retry(블록 하나 다시 처리)가 같은 코드를 쓴다.
  둘이 다른 코드를 쓰면 retry 결과만 형식이 달라지는 버그가 생기기 쉽다.
- 블록 하나에서 오류가 나도 try/except 로 그 블록만 실패로 기록하고 다음 블록을 계속 처리한다.
  [규칙 1] 실패한 블록도 지우거나 건너뛰지 않고 그 자리(같은 block_id/order)에 실패 상태로 남긴다.
"""
from app.logger import log_ctx, logger
from app.pipeline.processors import PROCESSORS
from app.pipeline.processors.common import ProcessContext, failed_result, now
from app.pipeline.storage import read_json, write_json
from app.pipeline.structure import IDENTITY_FIELDS, IdentityChangedError, set_block_status


def process_block(ctx: ProcessContext, block: dict) -> dict:
    """
    블록 하나를 처리하고 결과를 저장한다.
    받는 값: 처리 상자, structure 의 블록
    돌려주는 값: 저장된 블록 결과 dict
    """
    try:
        result = PROCESSORS[block["type"]](ctx, block)
    except Exception as e:
        # 오류가 나도 이 블록만 실패로 남긴다 (나머지 블록 처리는 계속). traceback 은 로그로 남긴다
        logger.error(
            f"블록 처리 실패 {block['block_id']}: {type(e).__name__}: {e}",
            exc_info=True,
            extra=log_ctx(ctx.storage.doc_id, ctx.structure.get("file_name"), "process"),
        )
        result = failed_result(block, "error", f"{type(e).__name__}: {e}")

    previous = read_json(ctx.storage.block_result(block["block_id"])) or {}
    # 사용자가 고친 내용(edited_text)은 다시 처리해도 지우지 않는다. 사람이 직접 한 작업이기 때문
    result["edited_text"] = previous.get("edited_text")
    result["attempts"] = previous.get("attempts", 0) + 1
    result["processed_at"] = result.get("processed_at") or now()

    # [규칙 1] 처리기가 실수로 번호를 바꿨는지 마지막으로 확인한다
    for f in IDENTITY_FIELDS:
        if result.get(f) != block.get(f):
            raise IdentityChangedError(f"{block['block_id']} 의 {f} 가 처리 중에 바뀌었습니다")

    write_json(ctx.storage.block_result(block["block_id"]), result)
    set_block_status(ctx.storage, {block["block_id"]: result["status"]})
    return result


def run_processing(ctx: ProcessContext, on_progress=None) -> list[dict]:
    """
    모든 블록을 order 순서로 처리한다.
    on_progress(done, total): 블록 하나가 끝날 때마다 불러서 진행률을 status.json 에 남긴다.
    """
    blocks = sorted(ctx.structure["blocks"], key=lambda b: b["order"])
    results = []
    for i, block in enumerate(blocks, start=1):
        results.append(process_block(ctx, block))
        if on_progress:
            on_progress(i, len(blocks))
    return results
