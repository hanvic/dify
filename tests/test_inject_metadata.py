"""
tests/test_inject_metadata.py

inject_metadata.py의 핵심 로직 단위 테스트:
- 문서명 파싱 (parse_doc_name)
- CP949 디코딩 및 index.db3 로딩
- 폴백 메타데이터 생성
- idempotency 검증
"""

import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

# inject_metadata 모듈 임포트를 위한 sys.path 설정
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from inject_metadata import parse_doc_name, try_fallback_metadata, load_index_db, inject_metadata


class TestParseDocName:
    """문서명 파싱 테스트"""

    def test_standard_pattern(self):
        """표준 패턴: {base_key}_p{N}.jpeg"""
        base_key, page = parse_doc_name("회계장부_2002~2004 기타장부_금전출납부 1 복지자금_p0.jpeg")
        assert base_key == "회계장부_2002~2004 기타장부_금전출납부 1 복지자금"
        assert page == 0

    def test_page_number_multidigit(self):
        """다자리 페이지 번호"""
        base_key, page = parse_doc_name("일반문서_1965~1981_정관_1965_p123.jpeg")
        assert base_key == "일반문서_1965~1981_정관_1965"
        assert page == 123

    def test_no_page_number(self):
        """페이지 번호 없는 경우"""
        base_key, page = parse_doc_name("some_document.jpeg")
        assert base_key == "some_document"
        assert page is None

    def test_jpg_extension(self):
        """.jpg 확장자"""
        base_key, page = parse_doc_name("회계장부_1974~1983_급여대장_1979_p5.jpg")
        assert base_key == "회계장부_1974~1983_급여대장_1979"
        assert page == 5

    def test_nfc_normalization(self):
        """NFD → NFC 정규화 (macOS 파일명)"""
        # NFD로 인코딩된 한글
        nfd_name = unicodedata.normalize("NFD", "회계장부_금전출납부_p0.jpeg")
        base_key, page = parse_doc_name(nfd_name)
        expected = unicodedata.normalize("NFC", "회계장부_금전출납부")
        assert base_key == expected
        assert page == 0

    def test_parentheses_in_name(self):
        """이름에 괄호 포함"""
        base_key, page = parse_doc_name("갑근세 기타서류(적립금)_1994~1999_1996 적립기금(수입, 지출)_1996_p2.jpeg")
        assert base_key == "갑근세 기타서류(적립금)_1994~1999_1996 적립기금(수입, 지출)_1996"
        assert page == 2


class TestFallbackMetadata:
    """폴백 메타데이터 생성 테스트"""

    def test_with_year_range(self):
        """연도 범위가 있는 경우"""
        result = try_fallback_metadata(
            "준공식 행사_2003~2005_준공식 사진",
            "준공식 행사_2003~2005_준공식 사진_p0.jpeg"
        )
        assert result is not None
        assert result["서류철명"] == "준공식 행사"

    def test_with_single_year(self):
        """단일 연도"""
        result = try_fallback_metadata(
            "행사_2003_개관식",
            "행사_2003_개관식_p0.jpeg"
        )
        assert result is not None
        assert result["서류철명"] == "행사"

    def test_no_year_fallback(self):
        """연도 없이 _ 분리"""
        result = try_fallback_metadata(
            "준공식 행사_준공식 사진",
            "준공식 행사_준공식 사진_p0.jpeg"
        )
        assert result is not None
        assert result["서류철명"] == "준공식 행사"
        assert result["문서명"] == "준공식 사진"


class TestLoadIndexDb:
    """index.db3 로딩 테스트 (임시 DB 사용)"""

    def _create_test_db(self, tmp_path: Path) -> Path:
        """테스트용 CP949 인코딩 SQLite DB 생성 (실제 DB처럼 TEXT 컬럼에 CP949 bytes 저장)"""
        db_path = tmp_path / "test_index.db3"
        # text_factory를 bytes로 설정하여 CP949 바이트를 TEXT 컬럼에 그대로 삽입
        conn = sqlite3.connect(str(db_path))
        conn.text_factory = bytes
        conn.execute("""
            CREATE TABLE index_info (
                id INTEGER PRIMARY KEY,
                header_1 TEXT, header_2 TEXT,
                header_3 TEXT, header_4 TEXT, header_5 TEXT,
                header_6 TEXT, header_7 TEXT, header_8 TEXT,
                header_9 TEXT, header_10 TEXT, header_11 TEXT
            )
        """)
        # CP949로 인코딩하여 TEXT 컬럼에 삽입
        test_data = [
            ("회계장부 1", "1965~1973", "급여대장", "1965", "김철수",
             "희망브리지 문서전자화(2025.04)\\회계장부 1_1965~1973\\급여대장_1965_김철수.pdf"),
            ("일반문서", "1982~1993", "정관", "1985", "",
             "희망브리지 문서전자화(2025.04)\\일반문서_1982~1993\\정관_1985.pdf"),
        ]
        for row in test_data:
            encoded = tuple(val.encode("cp949") for val in row)
            conn.execute(
                "INSERT INTO index_info (header_3, header_4, header_5, header_6, header_7, header_11) VALUES (?, ?, ?, ?, ?, ?)",
                encoded,
            )
        conn.commit()
        conn.close()
        return db_path

    def test_load_and_decode(self, tmp_path):
        """CP949 디코딩 및 키 생성 확인"""
        db_path = self._create_test_db(tmp_path)

        with patch("inject_metadata.INDEX_DB_PATH", db_path):
            mapping = load_index_db()

        assert len(mapping) == 2
        key1 = "회계장부 1_1965~1973_급여대장_1965_김철수"
        assert key1 in mapping
        assert mapping[key1]["서류철명"] == "회계장부 1"
        assert mapping[key1]["생산연도"] == "1965"
        assert mapping[key1]["작성자"] == "김철수"

    def test_key_matches_doc_name_parse(self, tmp_path):
        """DB의 키가 parse_doc_name 결과와 매칭되는지 확인"""
        db_path = self._create_test_db(tmp_path)

        with patch("inject_metadata.INDEX_DB_PATH", db_path):
            mapping = load_index_db()

        # 시뮬레이션: 문서명 → parse → 매핑 조회
        doc_name = "회계장부 1_1965~1973_급여대장_1965_김철수_p3.jpeg"
        base_key, page = parse_doc_name(doc_name)
        assert base_key in mapping
        assert page == 3


class TestIdempotency:
    """재실행 안전성 테스트"""

    def test_skip_existing_metadata(self):
        """이미 메타데이터가 있는 문서는 스킵"""
        field_ids = {
            "서류철명": "f1", "문서명": "f2",
            "생산연도": "f3", "작성자": "f4", "페이지번호": "f5",
        }
        index_mapping = {
            "test_folder_doc": {"서류철명": "test", "문서명": "doc", "생산연도": "2000", "작성자": "x"}
        }
        documents = [
            {
                "id": "doc1",
                "name": "test_folder_doc_p0.jpeg",
                "doc_metadata": [
                    {"id": "f1", "name": "서류철명", "value": "test"},
                    {"id": "f2", "name": "문서명", "value": "doc"},
                    {"id": "f3", "name": "생산연도", "value": "2000"},
                    {"id": "f4", "name": "작성자", "value": "x"},
                    {"id": "f5", "name": "페이지번호", "value": 0},
                ],
            }
        ]
        stats = inject_metadata(field_ids, index_mapping, documents, force=False, dry_run=True)
        assert stats["skipped_has_meta"] == 1
        assert stats["matched"] == 0

    def test_force_reinjection(self):
        """--force 옵션 시 이미 있는 문서도 재주입 대상"""
        field_ids = {
            "서류철명": "f1", "문서명": "f2",
            "생산연도": "f3", "작성자": "f4", "페이지번호": "f5",
        }
        index_mapping = {
            "test_folder_doc": {"서류철명": "test", "문서명": "doc", "생산연도": "2000", "작성자": "x"}
        }
        documents = [
            {
                "id": "doc1",
                "name": "test_folder_doc_p0.jpeg",
                "doc_metadata": [
                    {"id": "f1", "name": "서류철명", "value": "test"},
                    {"id": "f2", "name": "문서명", "value": "doc"},
                    {"id": "f3", "name": "생산연도", "value": "2000"},
                    {"id": "f4", "name": "작성자", "value": "x"},
                    {"id": "f5", "name": "페이지번호", "value": 0},
                ],
            }
        ]
        stats = inject_metadata(field_ids, index_mapping, documents, force=True, dry_run=True)
        assert stats["skipped_has_meta"] == 0
        assert stats["matched"] == 1
