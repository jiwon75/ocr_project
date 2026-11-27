"""
문서 처리 파이프라인 패키지.

전체 흐름 (꼭 이 순서로 실행된다)
  전처리(preprocess.py)  : 파일 형식별로 → 페이지 이미지(또는 HWP 파싱 결과)
  1단계(layout.py)       : 페이지마다 블록을 찾고, 읽기 순서를 정하고, block_id/order 를 붙인다 → structure.json
  2단계(process.py +
        processors/)     : 블록을 잘라 저장하고, 종류(text/title/table/image)별로 처리한다 → results/{block_id}.json
  3단계(merge.py)        : order 순서대로 블록 결과를 이어 붙인다 → .txt/.md + DB 저장
  runner.py              : 위 단계를 차례로 실행하고, 실패를 기록한다
  worker.py              : runner 를 별도 프로세스에서 돌리고, 시간 제한을 넘으면 그 프로세스를 끝낸다

API(app/api) 는 이 패키지의 함수만 부른다. 처리 로직과 API 를 나눠 두면,
나중에 worker.py 만 Celery/RQ 같은 작업 큐로 바꿔 끼울 수 있다.
"""
