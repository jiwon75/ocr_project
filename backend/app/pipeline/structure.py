"""
[이 파일이 하는 일]
structure.json(1단계 결과)을 읽고, 블록의 status(진행 상태)만 바꿔 저장한다.

[왜 따로 뺐는지 — 규칙 1]
[규칙 1] block_id 와 order 는 1단계에서만 정하고, 이후에는 절대 바꾸지 않는다.
2단계·retry·사용자 수정에서 structure.json 을 고칠 일이 생기면 반드시 이 파일의 함수만 쓰게 해서,
"status 외에는 못 바꾸는" 길 하나만 열어 둔다. 바꾸기 전후의 번호를 비교해서 달라졌으면 오류를 낸다.
"""
from app.pipeline.storage import DocStorage, read_json, write_json

# 1단계에서 정해진 뒤 절대 바뀌면 안 되는 항목들
IDENTITY_FIELDS = ("block_id", "order", "page", "type", "bbox")


class IdentityChangedError(RuntimeError):
    """[규칙 1] 을 어기려고 할 때 나는 오류. 이 오류가 났다면 코드에 버그가 있다는 뜻이다."""


def identity_of(blocks: list[dict]) -> list[tuple]:
    """블록 목록에서 '바뀌면 안 되는 값'만 뽑아낸다. 비교용."""
    return [tuple(_freeze(b.get(f)) for f in IDENTITY_FIELDS) for b in blocks]


def _freeze(v):
    return tuple(v) if isinstance(v, list) else v


def load_structure(storage: DocStorage) -> dict | None:
    return read_json(storage.structure_json)


def save_new_structure(storage: DocStorage, structure: dict) -> None:
    """1단계에서만 부른다. 블록 번호가 처음 정해지는 유일한 저장."""
    write_json(storage.structure_json, structure)


def set_block_status(storage: DocStorage, statuses: dict[str, str]) -> dict:
    """
    블록의 status 만 바꾼다. 예) set_block_status(st, {"p1-b003": "done"})
    [규칙 1] block_id/order 등은 건드리지 않으며, 혹시 바뀌었으면 저장하지 않고 오류를 낸다.
    """
    structure = read_json(storage.structure_json)
    before = identity_of(structure["blocks"])
    for block in structure["blocks"]:
        if block["block_id"] in statuses:
            block["status"] = statuses[block["block_id"]]
    if identity_of(structure["blocks"]) != before:
        raise IdentityChangedError("block_id/order 가 바뀌었습니다")
    write_json(storage.structure_json, structure)
    return structure


def find_block(structure: dict, block_id: str) -> dict | None:
    return next((b for b in structure["blocks"] if b["block_id"] == block_id), None)
