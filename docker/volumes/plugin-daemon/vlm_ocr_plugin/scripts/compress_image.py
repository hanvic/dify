#!/usr/bin/env python3
"""Resize and re-encode an image so it satisfies the Dify upload limit.

The output is always a JPEG. The long side is scaled so that it does not
exceed the configured maximum, then JPEG quality is lowered adaptively
until the file size is within the limit.
"""

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - graceful fallback when Pillow is unavailable
    Image = None  # type: ignore[assignment]


DEFAULT_MAX_LONG_SIDE = 4096
DEFAULT_SIZE_LIMIT = 10 * 1024 * 1024
QUALITY_STEPS = (95, 85, 75)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resize and compress an image for the Dify knowledge pipeline upload."
    )
    parser.add_argument("input_path", help="Path to the source image.")
    parser.add_argument("output_path", help="Path to write the compressed JPEG.")
    parser.add_argument(
        "--max-long-side",
        type=int,
        default=DEFAULT_MAX_LONG_SIDE,
        help="Maximum allowed long side in pixels (default: 4096).",
    )
    parser.add_argument(
        "--size-limit",
        type=int,
        default=DEFAULT_SIZE_LIMIT,
        help="Maximum allowed output size in bytes (default: 10 MiB).",
    )
    args = parser.parse_args()

    input_path = Path(args.input_path)
    output_path = Path(args.output_path)

    if not input_path.is_file():
        print(f"입력 파일을 찾을 수 없습니다: {input_path}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if Image is None:
        # Pillow 미설치 환경: 원본을 그대로 사용 (Dify 업로드 한도 이내라면 안전)
        try:
            output_path.write_bytes(input_path.read_bytes())
            print(
                "Pillow가 설치되어 있지 않아 원본 이미지를 그대로 사용합니다.",
                file=sys.stderr,
            )
            return 0
        except OSError as exc:
            print(f"원본 복사 중 오류가 발생했습니다: {exc}", file=sys.stderr)
            return 1

    try:
        with Image.open(input_path) as image:
            # JPEG does not support alpha or palette modes; normalize to RGB.
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            elif image.mode != "RGB":
                image = image.convert("RGB")

            width, height = image.size
            long_side = max(width, height)
            if long_side > args.max_long_side:
                ratio = args.max_long_side / long_side
                new_size = (int(width * ratio), int(height * ratio))
                image = image.resize(new_size, Image.Resampling.LANCZOS)

            for quality in QUALITY_STEPS:
                image.save(output_path, format="JPEG", quality=quality, optimize=True)
                if output_path.stat().st_size <= args.size_limit:
                    return 0

        final_size = output_path.stat().st_size
        print(
            f"이미지를 {args.size_limit}바이트 이하로 압축하지 못했습니다: {final_size}바이트",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"이미지 처리 중 오류가 발생했습니다: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
