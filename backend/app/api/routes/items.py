"""예제 CRUD 라우터 (메모리 저장소). 실제 DB 연동 시 교체하세요."""
from fastapi import APIRouter, HTTPException

from app.schemas.item import Item, ItemCreate

router = APIRouter()

_items: dict[int, Item] = {}
_next_id = 1


@router.get("", response_model=list[Item])
def list_items():
    return list(_items.values())


@router.post("", response_model=Item, status_code=201)
def create_item(payload: ItemCreate):
    global _next_id
    item = Item(id=_next_id, **payload.model_dump())
    _items[item.id] = item
    _next_id += 1
    return item


@router.get("/{item_id}", response_model=Item)
def get_item(item_id: int):
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    return _items[item_id]


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: int):
    if _items.pop(item_id, None) is None:
        raise HTTPException(status_code=404, detail="Item not found")
