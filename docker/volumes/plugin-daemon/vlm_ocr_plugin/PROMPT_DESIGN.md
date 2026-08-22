# VLM OCR Prompt Design

This document describes the design of the Korean VLM OCR prompts used by the
`vlm_ocr_plugin` Dify tool.

## Goals

Enterprise document scans in Korea often contain:

- Mixed Korean/English text with legal or financial terminology.
- Tables, forms, and line items that must keep their original structure.
- Official stamps, seals, signatures, charts, and diagrams.
- Hierarchical numbering, headers, footers, and multi-column layouts.
- Low-quality scans, skewed pages, or compressed A4 images.

The prompts are designed to maximize extraction accuracy while minimizing
hallucination and inconsistent formatting.

## English meta-rule and optional system message

Every prompt starts with a short English meta-rule:

```text
Extract all visible text verbatim, regardless of language. Output must be in Korean, formatted as pure markdown. No HTML, JSON, XML, or code fences.
```

VLMs often follow the first line of a prompt most reliably. Placing the core
intent in English reduces the chance that the model emits unwanted formats.

The same English line and a condensed set of high-level meta-rules (role,
pure markdown, verbatim extraction, no hallucination, required prefixes,
header/footer tags) are returned as the system prompt, while the detailed
extraction rules (sections 1-7 below) live in the user prompt.
`tools.prompts.build_ocr_prompt` exposes this through the `include_system=True`
flag so callers using chat-style APIs such as Ollama `/api/chat` can pass
`system` and `user` separately. The default behavior still returns a single string
for backward compatibility.

## Why this structure?

The default prompt is split into numbered sections for two reasons:

1. **VLMs follow enumerated instructions well.** A clear list reduces the chance
   that the model skips a requirement or mixes it with the extracted text.
2. **Each section maps to a common failure mode.** Ordering text extraction
   before structure, tables, and figures ensures the model first captures the
   raw content and then formats it correctly.

### Section-by-section rationale

#### 1. Text extraction

- Korean enterprise documents rely on exact wording for compliance. The prompt
  instructs the model to "원문을 그대로 유지하고, 의역이나 번역하지 마세요." so
  the verbatim requirement applies to any visible language and stays consistent
  with the English meta-rule "regardless of language." Requiring "그대로"
  (verbatim) extraction prevents the model from paraphrasing company names,
  amounts, or registration numbers.
- Signatures and stamps are treated with a clear priority: if characters are
  legible inside the stamp or signature, transcribe them verbatim; otherwise
  describe the visual stamp/signature in Korean rather than hallucinating text.

#### 2. Table reconstruction

- Markdown tables are widely supported and compact.
- Keeping empty cells as `|` preserves the original column alignment, which is
  critical for invoices, tax forms, and specification tables.
- Markdown pipe tables cannot contain raw line breaks inside cells. To stay
  valid without introducing HTML, the prompt instructs the model to join
  multi-line cell content with spaces into a single line. This preserves all
  visible text, avoids invalid table syntax, and keeps the output free of HTML.
- Markdown meta-characters such as `|` and `*` that appear inside cell content
  are escaped with a leading backslash so they do not break table syntax.
- Merged cells are filled by repeating the value by default. Empty cells
  (`|`) are used only where a column merge leaves no value, so the original
  table geometry is still recoverable.
- If a table is too complex for valid markdown, the model falls back to
  row-by-row text with clear separators instead of producing a broken table.
- Visible table captions or titles are always included because they are needed
  for downstream retrieval and understanding.

#### 3. Figure/chart/diagram description

- Charts and seals carry meaning but cannot always be transcribed as text.
  A structured description captures visible labels, values, and relationships.
- Requiring the model to quote visible numbers and avoid speculation prevents
  invented values.
- Visual elements are tagged with prefixes (`그림:`, `도표:`, `도장 영역:`) so
  downstream processing can distinguish extracted text from generated
  descriptions.

#### 4. Document structure preservation

- Korean reports and circulars use deep numbering (`1.`, `1.1`, `가.`, `(1)`).
  Preserving hierarchy makes downstream parsing and chunking reliable.
- Headers and footers are tagged with `(헤더)` and `(바닥글)` respectively,
  making it easy to strip or process them separately.
- Page separators (`페이지 N`) are inserted when the input spans multiple
  pages, which simplifies later reconstruction.

#### 5. Image quality and distortion

- Scans may be rotated, skewed, stained, or wrinkled. The prompt tells the model
  to read the document as if it were correctly oriented and to ignore physical
  defects.
- Resized or compressed images can introduce blur and compression artifacts.
  The model must not infer content from those artifacts; only clearly visible
  text is emitted.

#### 6. Hallucination guard

- Explicitly forbidding guessing or rewriting is necessary because VLMs often
  "fill in" blurry characters.
- `[판독 불가]` is reserved for completely unreadable text. Slightly blurry but
  still decipherable characters should be transcribed as best as possible,
  reducing over-use of the fallback marker.

#### 7. Output format

- Restricting output to pure markdown keeps the result usable by downstream RAG
  and workflow nodes without cleanup.
- The prohibition on wrapper code blocks avoids JSON/HTML fences that break
  markdown consumers.

## Expected output format

The VLM should return a single markdown document. A typical result looks like:

```markdown
# 품의서

(헤더) ABC주식회사 | 품의서

## 1. 기본 정보

- 작성일: 2025년 7월 17일
- 작성부서: 영업팀
- 담당자: 김OO

## 2. 내용

(1) 건명: 3분기 영업 현황 보고
(2) 세부 내용:

| 항목 | 계획 | 실적 | 비고 |
|------|------|------|------|
| 매출 | 10억 | 11억 | 초과 달성 |
| 영업이익 | 1억 | 0.9억 | 부족 |

## 3. 결재

- 기안: 김OO
- 검토: 이OO
- 승인: 박OO

도장 영역: 원형 직인, "ABC주식회사" 텍스트가 안쪽 원에 가로로 배치됨

(바닥글) 페이지 1 / 3
```

## Customization

Use the helper functions in `tools/prompts.py` to adapt the prompt without
editing `DEFAULT_OCR_PROMPT` directly.

### Add per-document instructions

```python
from tools.prompts import build_ocr_prompt

prompt = build_ocr_prompt(
    image_metadata={"filename": "invoice_20250717.pdf", "page": 1},
    extra_instructions="품목별 단가와 부가세를 별도 열로 추출하세요.",
)
```

### Use a separate system message

For chat APIs that accept a system message, such as Ollama `/api/chat`:

```python
from tools.prompts import build_ocr_prompt

system_prompt, user_prompt = build_ocr_prompt(include_system=True)

payload = {
    "model": "your-model",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ],
}
```

### Explain a resized image

```python
from tools.prompts import build_ocr_prompt, build_resize_note

resize_note = build_resize_note(target_width=1600, target_height=2263)
prompt = build_ocr_prompt(extra_instructions=resize_note)
```

### Add domain-specific rules

For financial documents, append instructions such as:

```python
extra = """
- 금액은 쉼표와 원화 기호(₩)를 그대로 출력하세요.
- 공급가액, 세액, 합계금액이 표에 있으면 별도 행으로 구분하세요.
"""
prompt = build_ocr_prompt(extra_instructions=extra)
```

## Extending the prompt

If you need a completely new default prompt, consider:

1. Keeping the same seven-section layout for consistency.
2. Translating the output language requirement if the target VLM handles
   multilingual output better in another language.
3. Adding a few-shot example in the prompt when the document class is very
   narrow (e.g. only Korean national tax invoices).

Avoid overloading the prompt with too many constraints; VLMs perform better
when each instruction is concrete and testable against the image.

## Related documentation

- [README.md](./README.md) — plugin overview, installation, and troubleshooting
- [DECISIONS.md](./DECISIONS.md) — architecture and design decisions
- [PIPELINE_SETUP_GUIDE.md](./PIPELINE_SETUP_GUIDE.md) — headless knowledge pipeline setup
