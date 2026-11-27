"""
[이 파일이 하는 일]
PaddleOCR 모델들을 불러오고, 파이프라인이 쓰기 쉬운 함수 4개로 감싼다.
  detect_layout(page)     → 페이지 안의 블록 위치와 종류 (1단계)
  detect_text(crop)       → 블록 안의 글자 줄 위치 (2단계)
  recognize(lines, lang)  → 줄 이미지 → 글자 + confidence (2단계)
  table_structure(crop)   → 표의 행/열/병합 구조와 셀 위치 (2단계)

[왜 이렇게 나눴는지]
- 파이프라인 코드가 PaddleOCR 사용법을 직접 알면, 나중에 다른 OCR(Surya 등)로 바꿀 때 모든 파일을 고쳐야 한다.
  이 클래스만 같은 함수 이름으로 다시 만들면 나머지 코드는 그대로 쓸 수 있다.
- 테스트에서는 이 클래스 대신 "가짜 엔진"(tests/fakes.py)을 넣어, 모델 없이도 규칙을 빠르게 검사한다.

[모델 선택 이유] (이 PC: CPU i5-6500, GPU 사용 불가 기준으로 직접 측정)
- 레이아웃: PP-DocLayout_plus-L — S 모델은 제목·문단을 놓쳤고, plus-L 은 모두 찾았다 (페이지당 약 2초)
- 글자 검출: PP-OCRv5_mobile_det — 블록당 0.1초 수준으로 빠르다
- 글자 인식: korean_PP-OCRv5_mobile_rec(한국어+영어) / en_PP-OCRv5_mobile_rec(영어 전용). 둘 다 미리 불러 둔다
- 표 구조: SLANet_plus — 병합 셀(colspan/rowspan)까지 HTML 구조로 알려 준다
- PP-StructureV3 전체 파이프라인은 방향 보정·수식·도장 모델까지 한꺼번에 돌려서 이 CPU 로는 30초 안에 끝내기 어렵고,
  블록 순서를 자체적으로 정해 버려서 [규칙 1](우리가 1단계에서 번호를 정함)과 맞지 않아 모듈별로 나눠 쓴다.

용어
- bbox(bounding box): 블록을 감싸는 사각형 좌표 [x1, y1, x2, y2]. (x1,y1)=왼쪽 위, (x2,y2)=오른쪽 아래, 단위는 픽셀
"""
import os

import numpy as np

# 언어 선택값 → 사용할 인식 모델. auto 는 두 모델을 모두 돌려 줄마다 점수가 높은 쪽을 고른다
LANGUAGES = ("auto", "ko", "en")


class PaddleEngines:
    """
    모든 OCR 모델을 한 번에 불러 들고 있는 객체.
    워커 프로세스가 시작할 때 딱 한 번 만들어서, 그 워커가 처리하는 모든 파일에 재사용한다.
    요청마다 새로 만들면 매번 모델 파일을 읽고 초기화하느라 십수 초씩 걸리기 때문.
    """

    def __init__(self, settings):
        if settings.PADDLE_DISABLE_MODEL_SOURCE_CHECK:
            # PaddleX 는 시작할 때마다 모델 다운로드 서버에 접속 확인을 한다. 이미 받은 모델만 쓰므로 건너뛴다
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        # import 를 함수 안에서 하는 이유: paddle 을 불러오는 것 자체가 몇 초 걸리고 메모리를 많이 써서,
        # 모델이 필요 없는 API 서버 프로세스나 가벼운 테스트에서는 불러오지 않기 위해서다
        from paddleocr import LayoutDetection, TableStructureRecognition, TextDetection, TextRecognition

        self.layout_model = LayoutDetection(model_name=settings.LAYOUT_MODEL, threshold=settings.LAYOUT_THRESHOLD)
        self.det_model = TextDetection(model_name=settings.TEXT_DET_MODEL)
        # 한국어/영어 모델을 둘 다 미리 불러 둔다. 요청이 왔을 때 불러오면 그 요청만 몇 초 더 느려지기 때문
        self.rec_models = {
            "ko": TextRecognition(model_name=settings.TEXT_REC_MODEL_KO),
            "en": TextRecognition(model_name=settings.TEXT_REC_MODEL_EN),
        }
        self.table_model = TableStructureRecognition(model_name=settings.TABLE_MODEL)
        self._warm_up()

    def _warm_up(self):
        """
        작은 빈 이미지로 모든 모델을 한 번씩 돌려 본다.
        모델은 첫 번째 실행 때 내부 준비 작업을 해서 유난히 느리다. 그 시간을 첫 사용자 요청(30초 제한)이 아니라
        서버 시작 시간에 쓰기 위해서다.
        """
        blank = np.full((64, 256, 3), 255, dtype=np.uint8)
        self.detect_layout(blank)
        self.detect_text(blank)
        self.recognize([blank], "auto")
        self.table_structure(blank)

    @staticmethod
    def _bgr(image_rgb: np.ndarray) -> np.ndarray:
        """PaddleOCR 은 OpenCV 방식(BGR 색 순서)을 기대한다. 우리는 RGB 로 다루므로 순서를 뒤집어 넘긴다."""
        return np.ascontiguousarray(image_rgb[:, :, ::-1])

    def detect_layout(self, page_rgb: np.ndarray) -> list[dict]:
        """
        받는 값: 페이지 이미지 (RGB numpy 배열)
        돌려주는 값: [{"label": "text", "score": 0.93, "bbox": [x1, y1, x2, y2]}, ...]  (순서는 아직 의미 없음)
        """
        # layout_nms=True: 서로 크게 겹치는 중복 박스를 하나로 정리한다. 안 하면 같은 문단이 두 블록으로 잡혀 글자가 두 번 나온다
        res = self.layout_model.predict(self._bgr(page_rgb), batch_size=1, layout_nms=True)
        boxes = res[0].json["res"]["boxes"] if res else []
        return [{"label": b["label"], "score": float(b["score"]), "bbox": [float(v) for v in b["coordinate"]]} for b in boxes]

    def detect_text(self, crop_rgb: np.ndarray) -> list[list[float]]:
        """
        받는 값: 블록 crop 이미지
        돌려주는 값: 글자 줄 위치 목록 [[x1, y1, x2, y2], ...] (crop 기준 좌표)
        """
        res = self.det_model.predict(self._bgr(crop_rgb), batch_size=1)
        polys = res[0].json["res"]["dt_polys"] if res else []
        boxes = []
        for poly in polys:
            p = np.asarray(poly, dtype=float)
            # 검출 결과는 기울어진 사각형(점 4개)이다. 문서는 대부분 수평이므로 감싸는 직사각형으로 바꿔 쓴다
            boxes.append([float(p[:, 0].min()), float(p[:, 1].min()), float(p[:, 0].max()), float(p[:, 1].max())])
        return boxes

    def recognize(self, line_images: list[np.ndarray], language: str) -> list[dict]:
        """
        받는 값: 줄 이미지 목록, 언어("auto"|"ko"|"en")
        돌려주는 값: 줄마다 {"text": "...", "confidence": 0.97, "lang": "ko", "candidates": {...}}
          candidates: auto 일 때 두 모델의 결과를 모두 남긴다. 나중에 왜 그 결과가 골라졌는지 확인할 수 있게.
        """
        if not line_images:
            return []
        images = [self._bgr(im) for im in line_images]
        langs = ["ko", "en"] if language == "auto" else [language]
        per_lang = {}
        for lang in langs:
            # 줄 여러 개를 한 번에(batch) 넘긴다. 한 줄씩 부르는 것보다 훨씬 빠르다
            res = self.rec_models[lang].predict(images, batch_size=len(images))
            per_lang[lang] = [(r.json["res"]["rec_text"], float(r.json["res"]["rec_score"])) for r in res]

        results = []
        for i in range(len(images)):
            cands = {lang: {"text": per_lang[lang][i][0], "confidence": per_lang[lang][i][1]} for lang in langs}
            # 한국어·영어가 섞인 문서도 줄마다 더 자신 있는 모델의 결과를 고른다.
            # 한국어 줄을 영어 모델로 읽으면 엉터리 글자와 낮은 점수가 나오므로 자연스럽게 걸러진다
            best = max(langs, key=lambda lang: cands[lang]["confidence"])
            results.append({**cands[best], "lang": best, "candidates": cands if language == "auto" else None})
        return results

    def table_structure(self, crop_rgb: np.ndarray) -> dict:
        """
        받는 값: 표 crop 이미지
        돌려주는 값: {"tokens": ["<tr>", "<td></td>", ...], "cells": [[x1,y1,x2,y2], ...], "score": 0.99}
          tokens: 표 구조를 HTML 조각으로 표현한 것. colspan/rowspan 이 들어 있다
          cells : tokens 의 <td> 순서와 같은 순서의 셀 위치 (crop 기준)
        """
        res = self.table_model.predict(self._bgr(crop_rgb), batch_size=1)
        if not res:
            return {"tokens": [], "cells": [], "score": 0.0}
        j = res[0].json["res"]
        cells = []
        for poly in j.get("bbox", []):
            xs, ys = poly[0::2], poly[1::2]
            cells.append([float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))])
        return {"tokens": list(j.get("structure", [])), "cells": cells, "score": float(j.get("structure_score", 0.0))}


def load_engines(settings) -> PaddleEngines:
    """워커가 시작할 때 부르는 함수. (테스트는 이 자리에 가짜 엔진을 만드는 함수를 넣는다)"""
    return PaddleEngines(settings)
