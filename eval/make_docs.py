"""
[이 파일이 하는 일]
모델 비교(OCR·LLM 요약·교정·청킹)에 쓸 가상 문서 5개와 정답(answers.json)을 만든다.

  python eval/make_docs.py                     → eval/docs/ 에 문서 5개 + answers.json
  python eval/make_docs.py --out "저장할 폴더"   → 원하는 폴더에 (예: 바탕화면 폴더)

[왜 이렇게 만들었는지]
- 실제 개인 문서(이력서 등)는 공유·캡처하기 곤란하다. 내용은 모두 지어낸 것이다.
- 모델을 비교하려면 "정답"이 있어야 한다. 그래서 문서를 직접 만들고, 문서에 넣은 글자 그대로를 정답으로 저장한다.
- 스캔본은 일부러 흐리게·기울게·잡음을 넣어 OCR 오타가 생기게 한다 → LLM 교정 능력을 비교할 수 있다.
- 글꼴은 PyMuPDF 에 들어 있는 CJK 글꼴을 써서 어느 PC 에서나 똑같이 만들어진다.
"""
import io
import json
import random
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter

DEFAULT_OUT = Path(__file__).resolve().parent / "docs"
FONT = pymupdf.Font("cjk")
W, H, MARGIN = 595, 842, 56  # A4 (pt)


# ---------------------------------------------------------------------
# PDF 쓰기 도우미
# ---------------------------------------------------------------------


class Writer:
    """위에서 아래로 글을 쓰고, 넘치면 새 페이지를 만든다. 쓴 글은 정답 텍스트(answer)에도 같은 순서로 모은다."""

    def __init__(self):
        self.doc = pymupdf.open()
        self.answer: list[str] = []
        self._new_page()

    def _new_page(self):
        self.page = self.doc.new_page(width=W, height=H)
        self.page.insert_font(fontname="kr", fontbuffer=FONT.buffer)
        self.y = MARGIN

    def _wrap(self, text: str, size: float, width: float) -> list[str]:
        """공백 기준으로 줄을 나누고, 한 단어가 너무 길면 글자 단위로 자른다."""
        lines, cur = [], ""
        for word in text.split(" "):
            cand = f"{cur} {word}" if cur else word
            if FONT.text_length(cand, size) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
            cur = ""
            for ch in word:
                if FONT.text_length(cur + ch, size) > width:
                    lines.append(cur)
                    cur = ""
                cur += ch
        if cur:
            lines.append(cur)
        return lines

    def _ensure(self, height: float):
        if self.y + height > H - MARGIN:
            self._new_page()

    def text(self, text: str, size: float = 10.5, gap: float = 8, center: bool = False):
        for line in self._wrap(text, size, W - 2 * MARGIN):
            self._ensure(size * 1.6)
            x = (W - FONT.text_length(line, size)) / 2 if center else MARGIN
            self.page.insert_text((x, self.y + size), line, fontname="kr", fontsize=size)
            self.y += size * 1.6
        self.y += gap
        self.answer.append(text)

    def title(self, text: str, size: float = 18):
        self.text(text, size, gap=14, center=True)

    def heading(self, text: str):
        self.y += 4
        self.text(text, 13, gap=6)

    def table(self, rows: list[list[str]], widths: list[float], size: float = 9.5):
        """한 줄짜리 칸으로 된 표. 칸 글자가 넘치지 않게 폭을 잡아 줘야 한다."""
        row_h = size * 2.2
        self._ensure(row_h * len(rows))
        for r, row in enumerate(rows):
            x = MARGIN
            for c, cell in enumerate(row):
                rect = pymupdf.Rect(x, self.y, x + widths[c], self.y + row_h)
                if r == 0:
                    self.page.draw_rect(rect, color=(0, 0, 0), fill=(0.9, 0.9, 0.9), width=0.6)
                else:
                    self.page.draw_rect(rect, color=(0, 0, 0), width=0.6)
                assert FONT.text_length(cell, size) < widths[c] - 6, f"칸 글자가 넘침: {cell}"
                self.page.insert_text((x + 4, self.y + row_h / 2 + size * 0.38), cell, fontname="kr", fontsize=size)
                x += widths[c]
            self.y += row_h
        self.y += 12
        self.answer.append("\n".join("\t".join(r) for r in rows))

    def pdf_bytes(self) -> bytes:
        self.doc.subset_fonts()  # 쓴 글자만 남겨 파일 크기를 줄인다 (한글 글꼴 전체는 수 MB)
        return self.doc.tobytes(garbage=3, deflate=True)


# ---------------------------------------------------------------------
# 스캔본 흉내
# ---------------------------------------------------------------------


def scan_like(page: pymupdf.Page, seed: int, dpi: int = 150, blur: float = 0.7, noise: float = 14, angle: float = 0.6) -> Image.Image:
    """페이지를 이미지로 바꾼 뒤 기울기·흐림·잡음·명암 저하를 넣어 스캔한 것처럼 만든다 (OCR 오타를 일부러 만들기 위해)."""
    rng = np.random.default_rng(seed)
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(blur))
    arr = np.asarray(img).astype(np.float32)
    arr = arr * 0.88 + 22  # 종이가 약간 회색, 글자가 약간 옅게
    arr += rng.normal(0, noise, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=55)  # JPEG 압축 흔적
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def images_to_pdf(images: list[Image.Image]) -> bytes:
    """이미지만 들어 있는(텍스트 레이어 없는) PDF = 스캔 PDF."""
    doc = pymupdf.open()
    for img in images:
        page = doc.new_page(width=W, height=H)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        page.insert_image(page.rect, stream=buf.getvalue())
    return doc.tobytes(garbage=3, deflate=True)


# ---------------------------------------------------------------------
# 문서 내용 (모두 가상)
# ---------------------------------------------------------------------


def doc1_notice():
    w = Writer()
    w.title("2026년 하반기 정보보안 교육 안내")
    w.text("샘플정보통신 경영지원본부", 10, center=True, gap=16)
    w.text("임직원 여러분께 하반기 정기 정보보안 교육 일정을 다음과 같이 안내드립니다. 이번 교육은 개인정보보호법 개정 사항과 "
           "최근 증가하고 있는 피싱 메일 대응 방법을 중심으로 진행됩니다.")
    w.heading("1. 교육 일정")
    w.text("일시: 2026년 12월 10일(목) 오후 2시부터 4시까지")
    w.text("장소: 본관 3층 대회의실")
    w.text("대상: 전 직원 (약 120명)")
    w.heading("2. 교육 내용")
    w.text("첫째, 개정된 개인정보보호법의 주요 내용과 위반 시 과태료 기준을 설명합니다. 둘째, 실제 피싱 메일 사례 15건을 함께 "
           "분석하고 의심 메일을 신고하는 절차를 실습합니다. 셋째, 업무용 노트북의 보안 설정을 점검합니다.")
    w.heading("3. 유의 사항")
    w.text("교육에 참석하지 못한 직원은 12월 24일까지 사내 학습 시스템에서 온라인 보충 교육을 반드시 이수해야 합니다. "
           "미이수자는 내년 1월 보안 점검 대상에 포함됩니다. 교육 당일에는 업무용 노트북을 지참해 주시기 바랍니다.")
    w.text("문의: 정보보안팀 (내선 2417)", gap=4)
    img = scan_like(w.doc[0], seed=1, dpi=110, blur=0.85, noise=22, angle=0.8)  # 오타가 생길 만큼 거칠게
    facts = ["12월 10일", "오후 2시", "3층 대회의실", "120명", "피싱", "12월 24일", "온라인 보충 교육", "2417"]
    return "01_보안교육_공지_스캔.png", img, w.answer, facts, "스캔 이미지(흐림·기울기·잡음). OCR 오타 → LLM 교정, 짧은 공지 요약"


def doc2_minutes():
    w = Writer()
    w.title("스마트 물류 시스템 구축 프로젝트 주간 회의록")
    w.table([["항목", "내용"],
             ["일시", "2026년 11월 24일(월) 10:00 ~ 11:30"],
             ["장소", "4층 프로젝트룸"],
             ["참석자", "김민준(PM), 이서연(백엔드), 박도윤(프론트엔드), 최지우(QA)"],
             ["작성자", "최지우"]], [90, 393])
    w.heading("1. 진행 현황")
    w.text("전체 진행률은 68%로 지난주 대비 9%p 상승했다. 입고 관리 모듈은 개발을 마치고 테스트 단계에 들어갔으며, "
           "재고 조회 화면은 디자인 검토가 끝나 이번 주부터 개발을 시작한다. 출고 관리 모듈은 외부 배송사 API 연동 문서가 "
           "늦게 도착해 일정이 약 1주 지연되었다.")
    w.text("백엔드 쪽에서는 재고 수량을 실시간으로 반영하는 과정에서 동시에 같은 품목을 출고할 때 수량이 음수가 되는 오류가 "
           "발견되었다. 이서연 님이 데이터베이스 잠금 방식을 바꾸는 방안을 제안했고, 수요일까지 수정 후 재테스트하기로 했다.")
    w.heading("2. 논의 사항")
    w.text("첫째, 배송사 API 지연에 따른 일정 조정을 논의했다. 출고 모듈의 1차 오픈 범위를 국내 택배 2개 사로 줄이고, "
           "화물 배송은 2차 오픈으로 미루기로 했다. 둘째, QA 인력 부족 문제를 논의했다. 테스트 케이스가 340개로 늘어나 "
           "현재 인원으로는 일정 내 완료가 어렵다는 의견이 있었고, 12월 한 달간 외부 테스트 인력 1명을 추가로 투입하기로 했다.")
    w.text("셋째, 남은 예산을 점검했다. 총예산 1억 2,000만 원 중 현재까지 8,750만 원을 집행했으며, 외부 테스트 인력 "
           "비용으로 1,250만 원을 추가 집행할 예정이다. 집행 후 잔액은 2,000만 원으로 예비비로 남겨 둔다.")
    w.heading("3. 결정 사항")
    w.text("출고 모듈 1차 오픈은 국내 택배 2개 사로 한정한다. 외부 테스트 인력 1명을 12월 1일부터 투입한다. "
           "재고 수량 음수 오류는 11월 26일까지 수정한다.")
    w.heading("4. 다음 일정")
    w.text("통합 테스트는 12월 8일에 시작하고, 1차 오픈 목표일은 12월 19일이다. 다음 회의는 12월 1일(월) 오전 10시에 같은 "
           "장소에서 진행한다.")
    facts = ["68%", "출고", "1주 지연", "음수", "국내 택배 2개", "외부 테스트 인력 1명", "1,250만", "12월 19일"]
    return "02_프로젝트_회의록.pdf", w.pdf_bytes(), w.answer, facts, f"디지털 PDF {len(w.doc)}쪽(텍스트 레이어, OCR 없음). OCR 오류 없는 상태의 요약 품질"


def doc3_quote():
    w = Writer()
    w.title("견  적  서", 22)
    w.table([["견적번호", "Q-2026-1127", "견적일자", "2026-11-27"],
             ["수신", "샘플대학교 정보전산원", "유효기간", "견적일로부터 30일"]], [80, 160, 80, 163])
    w.text("아래와 같이 견적합니다.", gap=10)
    w.table([["번호", "품명", "규격", "수량", "단가", "금액"],
             ["1", "노트북", "15인치 i5 16GB", "3", "1,180,000", "3,540,000"],
             ["2", "모니터", "27인치 QHD", "3", "320,000", "960,000"],
             ["3", "키보드·마우스 세트", "무선", "3", "45,000", "135,000"],
             ["4", "도킹 스테이션", "USB-C", "3", "89,000", "267,000"],
             ["5", "설치 및 설정", "현장 방문", "1식", "150,000", "150,000"]],
            [34, 120, 105, 40, 88, 96])
    w.table([["공급가액", "5,052,000"], ["부가세(10%)", "505,200"], ["합계 금액", "5,557,200"]], [387, 96])
    w.text("납품 기한: 발주 후 14일 이내", 10, gap=4)
    w.text("결제 조건: 납품 완료 후 30일 이내 계좌 이체", 10, gap=4)
    w.text("공급자: 샘플컴퓨터유통 영업팀", 10, gap=4)
    img = scan_like(w.doc[0], seed=3, blur=0.6, noise=10, angle=-0.4)
    facts = ["5,052,000", "505,200", "5,557,200", "노트북", "3", "14일", "30일"]
    return "03_견적서_표_스캔.png", img, w.answer, facts, "표 위주 스캔 이미지. 숫자·금액(쉼표)을 바꾸지 않는지, 표 인식"


def doc4_mixed():
    w = Writer()
    w.title("OCR 파이프라인 성능 점검 보고서")
    w.text("Performance Review of the Document OCR Pipeline", 12, center=True, gap=14)
    w.heading("1. 개요 (Overview)")
    w.text("본 보고서는 문서 OCR 파이프라인의 layout detection, reading order, text recognition 단계별 성능을 점검한 결과를 "
           "정리한 것이다. 평가에는 스캔 문서 50건과 디지털 PDF 30건, 총 80건을 사용했다.")
    w.heading("2. 평가 결과 (Results)")
    w.table([["Metric", "Before", "After", "변화"],
             ["Layout Precision", "0.86", "0.91", "+0.05"],
             ["Layout Recall", "0.82", "0.87", "+0.05"],
             ["Character Error Rate", "6.4%", "3.9%", "-2.5%p"],
             ["Avg. Latency (s/page)", "6.1", "4.8", "-1.3"]], [170, 90, 90, 133])
    w.text("레이아웃 검출의 precision 과 recall 이 모두 0.05 상승했고, 문자 오류율(CER)은 6.4%에서 3.9%로 낮아졌다. "
           "페이지당 평균 처리 시간도 6.1초에서 4.8초로 줄었다.")
    w.text("The main improvement came from lowering the text detection box threshold and enlarging the unclip ratio, "
           "which reduced missed single-digit numbers in tables.")
    w.heading("3. 남은 문제 (Known Issues)")
    w.text("첫째, 2단 편집 문서에서 각주(footnote)가 본문 중간에 끼어드는 reading order 오류가 12건 남아 있다. "
           "둘째, 손글씨가 섞인 문서는 recognition confidence 가 0.6 이하로 떨어져 대부분 추출 실패로 처리된다.")
    w.text("Handwritten notes are out of scope for this release and will be revisited in Q2 2027.")
    w.heading("4. 권고 사항 (Recommendations)")
    w.text("각주 영역을 별도 블록 유형으로 분리하고, 신뢰도 기준값(confidence threshold)은 현재 0.8을 유지할 것을 권고한다. "
           "다음 점검은 2027년 3월에 실시한다.")
    images = [scan_like(p, seed=40 + i, dpi=100, blur=0.9, noise=22, angle=0.7) for i, p in enumerate(w.doc)]
    facts = ["80건", "0.91", "0.87", "3.9%", "4.8", "각주", "손글씨", "0.8", "2027년 3월"]
    return "04_한영혼합_보고서_스캔.pdf", images_to_pdf(images), w.answer, facts, f"스캔 PDF {len(images)}쪽, 한국어+영어 혼합. 언어 자동 인식, 영어 용어 교정"


def doc5_long():
    w = Writer()
    w.title("지역 공공도서관 이용 활성화 방안 연구")
    w.text("(가상 연구 보고서 요약본)", 10, center=True, gap=16)
    w.heading("1. 연구 배경")
    w.text("가상의 도시 샘플시는 인구 42만 명 규모의 중소도시로, 공공도서관 9곳을 운영하고 있다. 최근 5년간 도서관 방문자 수는 "
           "2021년 182만 명에서 2025년 141만 명으로 약 23% 줄었다. 같은 기간 전자책 대출은 3배 가까이 늘었지만, 도서관을 "
           "직접 찾는 이용자는 꾸준히 감소하고 있다. 이에 따라 도서관이 단순히 책을 빌리는 곳에서 벗어나 지역 주민의 학습·문화 "
           "공간으로 역할을 넓혀야 한다는 요구가 커졌다.")
    w.text("본 연구는 샘플시 공공도서관의 이용 현황을 분석하고, 주민 설문과 해외 사례 검토를 바탕으로 이용 활성화 방안을 "
           "제시하는 것을 목적으로 한다.")
    w.heading("2. 이용 현황")
    w.table([["구분", "2021년", "2023년", "2025년"],
             ["연간 방문자 수", "182만 명", "158만 명", "141만 명"],
             ["종이책 대출 권수", "96만 권", "84만 권", "77만 권"],
             ["전자책 대출 건수", "11만 건", "22만 건", "31만 건"],
             ["프로그램 참여자", "3.2만 명", "4.1만 명", "5.6만 명"]], [150, 110, 110, 113])
    w.text("방문자와 종이책 대출은 줄었지만 도서관 프로그램 참여자는 오히려 75% 늘었다. 특히 성인 대상 디지털 교육과 어린이 "
           "독서 프로그램의 대기 인원이 정원의 두 배를 넘는 경우가 많았다. 이는 주민들이 도서관에 기대하는 역할이 대출에서 "
           "체험과 배움으로 옮겨 가고 있음을 보여 준다.")
    w.text("시간대별로 보면 평일 오후 6시 이후 방문자가 전체의 11%에 불과했다. 9곳 중 7곳이 평일 오후 6시에 문을 닫기 때문이다. "
           "직장인 응답자의 64%는 퇴근 후 이용할 수 없다는 점을 도서관을 찾지 않는 가장 큰 이유로 꼽았다.")
    w.heading("3. 주민 설문 결과")
    w.text("2025년 9월 주민 1,200명을 대상으로 설문을 실시했다. 도서관에 바라는 점으로는 운영 시간 연장(38%), 조용한 개인 학습 "
           "공간 확대(24%), 다양한 문화 프로그램(19%), 주차 공간 확보(11%) 순으로 응답이 많았다. 연령대별로는 20~30대가 운영 "
           "시간 연장을, 40대 이상이 문화 프로그램을 가장 많이 원했다.")
    w.text("도서관을 1년 이상 이용하지 않은 응답자에게 다시 이용할 의향을 물었을 때 57%가 운영 시간이 늘어나면 이용하겠다고 "
           "답했다. 반면 전자책만으로 충분하다는 응답은 13%에 그쳤다.")
    w.heading("4. 해외 사례")
    w.text("핀란드 헬싱키의 한 중앙도서관은 3D 프린터, 녹음실, 재봉틀 같은 장비를 갖춘 제작 공간을 운영해 개관 첫해에만 "
           "300만 명이 방문했다. 일본의 한 지방 도시는 도서관과 카페, 서점을 결합한 복합 공간으로 리모델링한 뒤 방문자가 "
           "4배 늘었다. 두 사례 모두 늦은 시간까지 문을 열고, 머무르기 좋은 공간을 만드는 데 투자했다는 공통점이 있다.")
    w.heading("5. 개선 방안")
    w.text("첫째, 운영 시간을 연장한다. 이용자가 많은 거점 도서관 3곳부터 평일 운영 시간을 오후 10시까지 늘리고, 무인 대출·반납기를 "
           "설치해 추가 인력 부담을 줄인다. 연간 추가 운영비는 약 4억 5,000만 원으로 추산된다.")
    w.text("둘째, 공간을 재구성한다. 서가 일부를 줄여 개인 학습석과 소규모 모임 공간을 만든다. 장서는 이용 빈도가 낮은 자료를 "
           "공동 보존 서고로 옮겨 공간을 확보한다.")
    w.text("셋째, 프로그램을 늘린다. 대기 인원이 많은 디지털 교육과 어린이 독서 프로그램의 정원을 두 배로 늘리고, 직장인을 위한 "
           "저녁 강좌를 새로 개설한다.")
    w.text("넷째, 홍보 방식을 바꾼다. 도서관 소식을 지역 커뮤니티 앱과 문자 알림으로 알리고, 전자책 이용자에게 오프라인 프로그램을 "
           "추천하는 기능을 도입한다.")
    w.heading("6. 기대 효과")
    w.text("운영 시간 연장과 공간 재구성을 함께 시행하면 3년 안에 연간 방문자 수를 2021년 수준인 180만 명까지 회복할 수 있을 것으로 "
           "예상된다. 또한 도서관이 주민의 학습과 교류 공간으로 자리 잡아 지역 공동체 활성화에도 기여할 것이다. 다만 추가 예산 확보와 "
           "야간 안전 관리 방안은 시의회 및 관련 부서와 별도로 협의해야 한다.")
    facts = ["23%", "141만", "프로그램 참여자", "오후 6시", "64%", "1,200명", "운영 시간 연장", "오후 10시", "4억 5,000만", "180만"]
    return "05_도서관_연구보고서.pdf", w.pdf_bytes(), w.answer, facts, f"디지털 PDF {len(w.doc)}쪽, 긴 문서. 청크 여러 개, 긴 문서 요약(부분 요약 → 합치기)"


# ---------------------------------------------------------------------


def main(out: Path = DEFAULT_OUT):
    random.seed(0)
    out.mkdir(parents=True, exist_ok=True)
    answers = []
    for make in (doc1_notice, doc2_minutes, doc3_quote, doc4_mixed, doc5_long):
        name, data, answer, facts, purpose = make()
        path = out / name
        if isinstance(data, Image.Image):
            data.save(path, "PNG", optimize=True)
        else:
            path.write_bytes(data)
        text = "\n\n".join(answer)
        missing = [f for f in facts if f not in text]
        assert not missing, f"{name}: 정답에 없는 핵심 사실 {missing}"
        answers.append({"file": name, "purpose": purpose, "key_facts": facts, "text": text})
        print(f"{name:30s} {path.stat().st_size / 1024:7.0f} KB  글자 {len(text):5d}  핵심 사실 {len(facts)}개")
    (out / "answers.json").write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    import argparse

    # 저장 위치는 PC 마다 다르므로 코드에 쓰지 않고 실행할 때 받는다
    parser = argparse.ArgumentParser(description="모델 비교용 가상 문서 만들기")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="저장할 폴더 (기본: eval/docs)")
    main(parser.parse_args().out)
