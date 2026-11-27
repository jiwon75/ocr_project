"""
[이 파일이 하는 일]
SQLite DB 연결, 테이블 생성, 저장/조회 함수를 모아 둔다.

[왜 이렇게 나눴는지]
SQL 문을 여기저기 흩어 두면 테이블 구조가 바뀔 때 고칠 곳을 찾기 어렵다.
DB 를 다루는 코드는 이 파일에만 두고, 다른 코드는 insert_uploaded_file() 같은 함수만 부른다.

용어
- SQLite: 파일 하나(data/app.db)로 동작하는 가벼운 DB. 별도 서버 설치가 필요 없다.
- FOREIGN KEY(외래 키): "이 칸의 값은 다른 테이블의 id 여야 한다"는 약속.
  extracted_texts.file_id 는 반드시 uploaded_files.id 중 하나여야 해서, 없는 파일을 가리키는 결과가 생기지 않는다.
- WAL(Write-Ahead Logging) 모드: 쓰는 내용을 먼저 별도 기록 파일에 적어 두는 방식.
  한 프로세스가 쓰는 동안에도 다른 프로세스가 읽을 수 있어서, API 서버와 워커가 동시에 DB 를 써도 덜 막힌다.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploaded_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT UNIQUE NOT NULL,
    file_name   TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    file_type   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extracted_texts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL,
    file_name       TEXT NOT NULL,
    extracted_text  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (file_id) REFERENCES uploaded_files(id)
);

-- file_id 로 자주 찾으므로 색인(index)을 만들어 둔다. 책의 "찾아보기"처럼 검색을 빠르게 해 준다
CREATE INDEX IF NOT EXISTS idx_extracted_texts_file_id ON extracted_texts(file_id);
"""


def _now() -> str:
    """저장 시각 문자열. 사람이 읽기 쉽고 정렬도 되는 ISO 형식(2026-11-27T16:40:01)을 쓴다."""
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path):
    """
    DB 연결을 열고, with 블록이 끝나면 저장(commit)하고 닫는다. 오류가 나면 되돌린다(rollback).

    연결을 전역 변수로 하나만 두지 않고 매번 새로 여는 이유:
    sqlite3 연결은 여러 프로세스(API 서버, 워커)가 나눠 쓸 수 없다. 프로세스마다 자기 연결을 따로 열어야 한다.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=10: 다른 프로세스가 쓰는 중이면 최대 10초 기다린다 (바로 "database is locked" 오류를 내지 않도록)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row  # 결과를 row["file_name"] 처럼 컬럼 이름으로 꺼낼 수 있게 한다
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # WAL 모드 (위 설명 참고)
        conn.execute("PRAGMA foreign_keys=ON")  # SQLite 는 기본으로 FOREIGN KEY 검사를 꺼 두므로 켜 준다
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    """테이블이 없으면 만든다. 서버 시작 시(lifespan) 한 번 부른다. IF NOT EXISTS 라서 여러 번 불러도 안전하다."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# 아래 함수들은 모두 값을 ? 자리표시자로 넣는다.
# 문자열을 직접 이어 붙여 SQL 을 만들면, 파일명에 ' 나 ; 같은 글자를 넣는 공격(SQL 인젝션)으로 DB 가 망가질 수 있기 때문.


def insert_uploaded_file(db_path: Path, doc_id: str, file_name: str, file_path: str, file_type: str) -> int:
    """
    업로드된 파일 정보를 한 줄 추가하고, 새로 생긴 id 를 돌려준다.
    이 id 가 나중에 extracted_texts.file_id 로 쓰인다.
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO uploaded_files (doc_id, file_name, file_path, file_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (doc_id, file_name, file_path, file_type, _now()),
        )
        return cur.lastrowid


def get_uploaded_file(db_path: Path, doc_id: str) -> dict | None:
    """doc_id 로 업로드 파일 정보를 찾는다. 없으면 None."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM uploaded_files WHERE doc_id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None


def save_extracted_text(db_path: Path, file_id: int, file_name: str, text: str) -> int:
    """
    3단계(합치기) 결과를 저장한다.
    - 같은 file_id 의 행이 이미 있으면 그 행을 갱신한다 (블록 retry/수정 후 다시 합칠 때)
    - 없으면 새로 추가한다
    파일명이 아니라 file_id 로 찾는 이유: 같은 이름의 파일이 여러 번 올라올 수 있어서 이름으로는 구분이 안 되기 때문.
    돌려주는 값: extracted_texts 의 id
    """
    with connect(db_path) as conn:
        row = conn.execute("SELECT id FROM extracted_texts WHERE file_id = ? ORDER BY id LIMIT 1", (file_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE extracted_texts SET extracted_text = ?, file_name = ?, created_at = ? WHERE id = ?",
                (text, file_name, _now(), row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO extracted_texts (file_id, file_name, extracted_text, created_at) VALUES (?, ?, ?, ?)",
            (file_id, file_name, text, _now()),
        )
        return cur.lastrowid


def delete_extracted_texts(db_path: Path, file_id: int) -> None:
    """
    파일 전체가 실패했을 때 혹시 남았을지 모르는 결과 행을 지운다.
    (예: 3단계에서 DB 저장 직후 시간 초과로 작업이 강제 종료된 경우)
    "실패한 파일은 extracted_texts 에 없어야 한다"는 요구사항을 확실히 지키기 위해서다.
    """
    with connect(db_path) as conn:
        conn.execute("DELETE FROM extracted_texts WHERE file_id = ?", (file_id,))


def get_extracted_texts(db_path: Path, file_id: int) -> list[dict]:
    """file_id 의 결과 행 목록 (보통 0개 또는 1개)."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM extracted_texts WHERE file_id = ? ORDER BY id", (file_id,)).fetchall()
        return [dict(r) for r in rows]
