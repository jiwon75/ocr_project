"""
가짜 OCR 엔진. 실제 모델 대신 "정해진 답"을 돌려준다.

왜 필요한가
- 실제 모델은 불러오는 데만 십수 초가 걸려서, 규칙·DB·시간 제한 테스트를 수십 번 돌리기엔 너무 느리다.
- 실제 모델의 confidence 는 그때그때 달라서 "confidence 가 낮으면 추출 실패" 같은 규칙을 정확히 검사하기 어렵다.
  가짜 엔진은 항상 같은 점수를 주므로 규칙이 맞게 동작하는지 정확히 확인할 수 있다.

동작 방식: tests/samples.py 의 make_fake_page() 가 블록마다 다른 회색으로 칠한 페이지를 만든다.
가짜 엔진은 이미지 가운데 픽셀 색을 보고 어떤 블록인지 알아내 미리 정한 글자·점수를 돌려준다.
"""
import os
import time

import numpy as np

from tests.samples import FAKE_BLOCKS, FAKE_PAGE_SIZE

# 회색값 → (인식 글자, confidence)
GRAY_TO_TEXT = {
    40: ("테스트 문서 제목", 0.99),
    80: ("첫 번째 문단입니다.", 0.97),
    120: ("흐릿한 글자", 0.42),  # 기준값(0.8)보다 낮음 → 추출 실패여야 함
    160: ("", 0.0),  # 빈 결과 → 추출 실패여야 함
    200: ("항목", 0.99),
    205: ("값", 0.98),
    210: ("사과", 0.97),
    215: ("흐린셀", 0.30),  # 표 안 낮은 confidence 셀
    230: ("그림 속 글자", 0.90),
}

RED, BLUE = (255, 0, 0), (0, 0, 255)

# 프로세스 안에서 한 번만 실패시키기 위한 기록 (retry 테스트용)
_flaky_state = {"failed_once": False}


def _gray(img: np.ndarray) -> int:
    h, w = img.shape[:2]
    return int(img[h // 2, w // 2, 0])


class FakeEngines:
    def __init__(self, settings=None, flaky: bool = False, load_delay: float = 0.0):
        if load_delay:
            time.sleep(load_delay)  # 모델 로딩이 오래 걸리는 상황 흉내 (health not_ready 테스트)
        self.flaky = flaky
        self.calls = {"layout": 0, "recognize": 0}

    def detect_layout(self, page: np.ndarray) -> list[dict]:
        self.calls["layout"] += 1
        corner = tuple(int(v) for v in page[0, 0])
        if corner == RED:
            time.sleep(120)  # 시간 제한보다 훨씬 오래 걸리는 파일
        if corner == BLUE:
            os._exit(3)  # 워커가 갑자기 죽는 상황 (메모리 부족 등)
        if (page.shape[1], page.shape[0]) != FAKE_PAGE_SIZE:
            return []  # 모르는 페이지 → 블록 없음 → 1단계가 페이지 전체를 블록 하나로 만든다
        # 일부러 읽기 순서와 다르게(거꾸로) 돌려준다. 1단계가 직접 순서를 정하는지 확인하기 위해서
        return [{"label": label, "score": 0.9, "bbox": list(map(float, bbox))} for label, bbox, _ in reversed(FAKE_BLOCKS)]

    def detect_text(self, crop: np.ndarray) -> list[list[float]]:
        h, w = crop.shape[:2]
        g = _gray(crop)
        if g in (200, 205, 210, 215) or 195 <= g <= 220:
            # 표: 4칸 각각의 가운데에 줄 하나씩
            boxes = []
            for cx, cy in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)):
                boxes.append([w * cx - 20, h * cy - 10, w * cx + 20, h * cy + 10])
            return boxes
        if g == 255:
            return []
        return [[w * 0.3, h * 0.4, w * 0.7, h * 0.6]]

    def recognize(self, images: list[np.ndarray], language: str) -> list[dict]:
        out = []
        for im in images:
            self.calls["recognize"] += 1
            g = _gray(im)
            if self.flaky and g == 80 and not _flaky_state["failed_once"]:
                _flaky_state["failed_once"] = True
                raise RuntimeError("가짜 엔진: 일시적 오류")
            text, score = GRAY_TO_TEXT.get(g, ("", 0.0))
            out.append({"text": text, "confidence": score, "lang": "ko", "candidates": None})
        return out

    def table_structure(self, crop: np.ndarray) -> dict:
        h, w = crop.shape[:2]
        pad = 12
        mx, my = w / 2, h / 2
        cells = [[pad, pad, mx, my], [mx, pad, w - pad, my], [pad, my, mx, h - pad], [mx, my, w - pad, h - pad]]
        tokens = ["<html>", "<body>", "<table>", "<tr>", "<td></td>", "<td></td>", "</tr>", "<tr>", "<td></td>", "<td></td>", "</tr>", "</table>", "</body>", "</html>"]
        return {"tokens": tokens, "cells": cells, "score": 0.99}


# 워커가 문자열 경로로 불러가는 "엔진 만드는 함수"들
def make_fake_engines(settings):
    return FakeEngines(settings)


def make_slow_loading_engines(settings):
    return FakeEngines(settings, load_delay=3.0)


def make_broken_engines(settings):
    raise RuntimeError("가짜 엔진: 모델 파일 없음")
