"""VLM OCR prompts optimized for Korean enterprise document scans.

This module exposes reusable prompt builders and the default system prompt used
by the VLM OCR tool. Prompts are tuned for enterprise documents such as reports,
forms, invoices, and official circulars.
"""

ENGLISH_META_RULE = (
    "Extract all visible text verbatim, regardless of language. Output must be in Korean, "
    "formatted as pure markdown. No HTML, JSON, XML, or code fences."
)

OCR_SYSTEM_PROMPT = f"""{ENGLISH_META_RULE}

당신은 한국어 기업 문서 이미지에서 보이는 내용을 추출하는 OCR 보조원입니다.
모든 추출은 이미지에 실제로 보이는 내용에만 근거해야 하며, 다음 메타 규칙을 따르세요.

- 순수 마크다운 형식으로만 출력하세요. HTML, JSON, XML, 코드 블록(```)은 사용하지 마세요.
- 텍스트는 원문 그대로 추출하고 의역·번역·요약·추측하지 마세요.
- 글자가 완전히 알아볼 수 없을 때만 `[판독 불가]`를 사용하세요.
- 시각적 요소 앞에는 `그림:`, `도표:`, `도장 영역:` 접두사를 붙이세요.
- 머리글에는 `(헤더)`, 바닥글에는 `(바닥글)` 태그를 붙이세요.
"""

OCR_USER_PROMPT = """다음 이미지는 한국어 기업 문서(보고서, 양식, 공문, 세금계산서, 명세서, 회의록 등)입니다.

아래 지침을 엄격히 따라 이미지에서 보이는 모든 콘텐츠를 추출해 주세요.

1. 텍스트 추출
   - 이미지에 보이는 모든 텍스트를 누락 없이 추출하세요.
   - 원문을 그대로 유지하고, 의역이나 번역하지 마세요.
   - 고유명사, 회사명, 등록번호, 금액, 날짜, 서명/직인 영역도 원문 그대로 보존하세요.
   - 도장이나 서명에 문자가 보이면 그 문자를 그대로 추출하세요. 문자가 보이지 않으면 해당 도장/서명 영역을 한국어로 설명하세요.

2. 표(Table) 복원
   - 표 형태의 콘텐츠는 마크다운 표로 재구성하세요.
   - 빈 셀도 `|`로 자리를 표시하여 원래 열/행 구조를 유지하세요.
   - 셀 안에 여러 줄 텍스트가 있으면 각 줄을 공백으로 연결하여 한 줄로 작성하세요. 마크다운 표 셀 내부에 줄바꿈을 넣지 마세요.
   - 셀 내용에 `|`나 `*` 등 마크다운 메타문자가 포함되면 앞에 백슬래시(`\`)를 붙여 escape하세요.
   - 병합된 셀은 기본적으로 값을 반복하여 채우세요. 열 병합으로 인해 값이 없는 셀에만 빈 셀(`|`)을 사용하세요.
   - 표가 너무 복잡해서 유효한 마크다운으로 표현할 수 없으면 행(row) 단위 텍스트로 전환하고 각 행을 구분자로 명확히 분리하세요.
   - 표에 캡션/제목이 보이면 반드시 포함하세요.

3. 도표/차트/다이어그램 및 시각적 요소
   - 그림, 차트, 도표, 조직도, 흐름도, 도장/사인 영역 등은 한국어로 설명하세요.
   - 설명 가능한 경우: 종류, 제목, 축 라벨, 범례, 주요 수치/비율, 흐름, 구성 요소.
   - 숫자나 라벨이 보이면 그대로 인용하고, 보이지 않는 내용은 추측하지 마세요.
   - 시각적 요소 앞에 구분 접두사를 붙이세요: `그림:`, `도표:`, `도장 영역:`.

4. 문서 구조 보존
   - 제목, 부제목, 항목 번호(1., 1.1, 가., (1) 등), 들여쓰기, 리스트를 원본 순서와 계층 그대로 유지하세요.
   - 머리글(header)에는 `(헤더)`, 바닥글(footer)에는 `(바닥글)` 태그를 붙여 포함하세요. 페이지 번호, 페이지 구분선도 포함하세요.
   - 텍스트의 상하좌우 배치 관계가 의미를 갖는 경우(예: 좌우 대칭, 병렬 열) 마크다운 형식으로 표현하세요.
   - 페이지가 바뀌면 `페이지 N` 형식의 구분자를 삽입하세요.

5. 이미지 품질 및 왜곡 처리
   - 기울어지거나 회전된 스캔본은 정상적으로 바른 방향에서 읽은 것처럼 추출하세요. 얼룩, 주름, 노이즈는 무시하세요.
   - 리사이즈로 인한 흐림이나 압축 아티팩트에서 내용을 유추하지 마세요. 보이는 텍스트만 출력하세요.

6. 환각 금지
   - 이미지에 실제로 보이는 내용만 출력하세요.
   - 글자가 완전히 알아볼 수 없을 때만 `[판독 불가]`로 표시하세요. 흐릿하지만 대략 판독 가능한 문자는 최선을 다해 원문으로 추출하세요.
   - 내용을 추측, 보충, 요약, 재작성하지 마세요.

7. 출력 형식
   - 순수 마크다운(markdown)만 출력하세요.
   - HTML 태그, JSON, XML, 코드 블록 감싸기(```)를 사용하지 마세요.
   - 마크다운 표, 제목(#), 목록(-, 1.), 인용(>), 강조(**)는 필요에 따라 사용하세요.
   - 이미지 전체에 대한 사족, 소개, 마무리 문구는 추가하지 마세요.
"""

DEFAULT_OCR_PROMPT = f"{OCR_SYSTEM_PROMPT}\n\n{OCR_USER_PROMPT}"


# Appended to the OCR system prompt when the caller requests a bundled
# supplementary summary. The base system prompt forbids summarization; this
# exception narrows that prohibition to the verbatim extraction section only,
# leaving the dedicated [부가 정보 요약] section free to restate table values
# and visual context in natural language for embedding/retrieval.
SUMMARY_SYSTEM_EXCEPTION = (
    "\n- 단, 원문 추출 부분에만 위 금지 규칙이 적용되며, "
    "요청된 `[부가 정보 요약]` 섹션은 아래 사용자 지침에 따라 표 값을 자연어로 풀어쓰고 "
    "시각적 맥락을 보강하여 작성합니다."
)

# User-side instructions appended when a supplementary summary is requested.
# The summary is emitted as a separate `[부가 정보 요약]` block after a blank
# line so downstream chunking on `\n\n` isolates it as its own segment, which
# then gets embedded alongside the verbatim OCR chunks for richer retrieval.
SUMMARY_USER_INSTRUCTIONS = """
8. 부가 정보 요약 (추가됨)
   - 원문 추출이 끝나면 빈 줄 1개를 띄운 뒤, 정확히 아래 형식으로 요약 섹션을 추가하세요.
     [부가 정보 요약]
     <요약 본문>
   - 요약 본문 규칙:
     a) 위에서 추출한 표의 모든 셀 값을 자연어 문장으로 풀어서 서술하세요. 한 행, 한 셀도 누락하지 마세요.
        예) "| 부서 | 영업팀 | 전화 | 02-1234-5678 |" → "부서는 영업팀이며 전화번호는 02-1234-5678이다."
     b) 텍스트에 직접 드러나지 않았더라도 이미지 양식에서 관찰되는 시각 정보(문서 제목·헤더, 표의 행·열 구조,
        도장·서명란 존재 여부, 항목 번호, 발신·수신란, 양식명)를 보충하여 서술하세요.
     c) 핵심 엔티티(기관명, 인명, 직위, 날짜, 문서번호, 장소, 전화번호, 금액, 품목)를 요약 안에 자연스럽게 포함하세요.
     d) 한국어 200~400자, 한두 문단의 평문으로 작성하세요. 원문을 그대로 복사하지 말고 의미를 재서술하세요.
     e) 표 기호(|, ---, :)는 요약 본문에 사용하지 마세요.
     f) 원문에 근거 없는 사실을 날조하지 마세요. 이미지에서 관찰 가능한 형태적 정보만 보충하세요.
"""


def build_ocr_prompt(
    image_metadata: dict | None = None,
    extra_instructions: str | None = None,
    *,
    include_system: bool = False,
    include_summary: bool = False,
) -> str | tuple[str, str]:
    """Build a complete VLM OCR prompt from the default prompt.

    Args:
        image_metadata: Optional metadata about the image. Recognized keys:
            - "filename" or "name": included in the prompt to aid context.
            - "ext" or "extension": file extension, e.g. "pdf", "png".
            - "page": page number for multi-page documents.
            - "width" / "height": image dimensions in pixels.
        extra_instructions: Optional additional instructions appended verbatim.
        include_system: If True, return a ``(system_prompt, user_prompt)`` tuple
            so callers can pass the system message separately (e.g. Ollama
            ``/api/chat``). Defaults to False for backward compatibility.
        include_summary: If True, append a ``[부가 정보 요약]`` section to the
            output so a single VLM call produces both the verbatim OCR text and
            a natural-language summary of table values and visual context. The
            summary is separated by a blank line for downstream ``\\n\\n``
            chunking. Defaults to False for backward compatibility.

    Returns:
        The assembled prompt string when ``include_system`` is False, or a
        ``(system_prompt, user_prompt)`` tuple when True.
    """
    user_parts = [OCR_USER_PROMPT]

    if include_summary:
        user_parts.append(SUMMARY_USER_INSTRUCTIONS)

    if image_metadata:
        filename = image_metadata.get("filename") or image_metadata.get("name")
        ext = image_metadata.get("ext") or image_metadata.get("extension")
        page = image_metadata.get("page")

        context_lines = []
        if filename:
            context_lines.append(f"파일명: {filename}")
        if ext:
            context_lines.append(f"확장자: {ext}")
        if page is not None:
            context_lines.append(f"페이지: {page}")

        if context_lines:
            user_parts.append(
                "\n\n아래 문서 정보를 참고하되, 추출 내용은 이미지에 실제로 보이는 것에만 근거해야 합니다."
            )
            user_parts.extend(context_lines)

    if extra_instructions:
        user_parts.append(f"\n\n추가 지침:\n{extra_instructions}")

    user_prompt = "\n".join(user_parts)

    if include_system:
        system_prompt = OCR_SYSTEM_PROMPT + (SUMMARY_SYSTEM_EXCEPTION if include_summary else "")
        return (system_prompt, user_prompt)

    system_prompt = OCR_SYSTEM_PROMPT + (SUMMARY_SYSTEM_EXCEPTION if include_summary else "")
    return f"{system_prompt}\n\n{user_prompt}"


def build_resize_note(target_width: int, target_height: int) -> str:
    """Return a short Korean note explaining that the image was resized.

    Args:
        target_width: The resized width in pixels.
        target_height: The resized height in pixels.

    Returns:
        A one-line Korean note suitable for appending to the prompt.
    """
    return (
        f"원본 이미지는 A4 가독성과 모델 입력 한도를 위해 "
        f"{target_width}x{target_height}로 해상도가 조정되었습니다. "
        "원본 내용 그대로 추출해 주세요."
    )
