from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from PIL import Image

from drawing_route_auditor.workflow.models import RenderedDrawing


class DrawingRenderError(RuntimeError):
    pass


def _rendered_pages(output_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in output_dir.glob("page-*.png")
            if path.stem.removeprefix("page-").isdigit()
        )
    )


def _drawing_frame_bounds(page: Path) -> tuple[int, int, int, int] | None:
    with Image.open(page) as image:
        grayscale = image.convert("L")
        width, height = grayscale.size
        pixels = grayscale.tobytes()

    minimum_dark_pixels = max(64, int(width * 0.12))
    frame_rows: list[int] = []
    row_extents: list[tuple[int, int]] = []
    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        dark_positions = [x for x, pixel in enumerate(row) if pixel < 128]
        if len(dark_positions) < minimum_dark_pixels:
            continue
        frame_rows.append(y)
        row_extents.append((dark_positions[0], dark_positions[-1]))

    if len(frame_rows) < 2:
        return None
    left = min(extent[0] for extent in row_extents)
    right = max(extent[1] for extent in row_extents) + 1
    top = frame_rows[0]
    bottom = frame_rows[-1] + 1
    crop_width = right - left
    crop_height = bottom - top
    if crop_width < width * 0.2 or crop_height < height * 0.2:
        return None
    if crop_width > width * 0.92 and crop_height > height * 0.92:
        return None

    horizontal_margin = max(16, int(width * 0.02))
    vertical_margin = max(16, int(height * 0.02))
    return (
        max(0, left - horizontal_margin),
        max(0, top - vertical_margin),
        min(width, right + horizontal_margin),
        min(height, bottom + vertical_margin),
    )


def prepare_reader_views(pages: tuple[Path, ...]) -> tuple[Path, ...]:
    views: list[Path] = []
    relative_regions = {
        "title": (0.38, 0.67, 1.0, 1.0),
        "geometry": (0.10, 0.08, 0.93, 0.74),
        "requirements": (0.0, 0.62, 0.58, 1.0),
    }
    for page in pages:
        detail_paths = {
            role: page.with_name(f"{page.stem}-{role}.png")
            for role in relative_regions
        }
        if not all(path.exists() for path in detail_paths.values()):
            with Image.open(page) as source:
                bounds = _drawing_frame_bounds(page)
                frame = source.crop(bounds) if bounds is not None else source.copy()
                width, height = frame.size
                for role, relative in relative_regions.items():
                    if detail_paths[role].exists():
                        continue
                    left, top, right, bottom = relative
                    detail = frame.crop(
                        (
                            round(width * left),
                            round(height * top),
                            round(width * right),
                            round(height * bottom),
                        )
                    )
                    detail.thumbnail((2600, 2000), Image.Resampling.LANCZOS)
                    detail.save(detail_paths[role], format="PNG", optimize=True)
        views.append(page)
        views.extend(detail_paths.values())
    return tuple(views)


async def render_pdf(
    pdf_path: Path,
    *,
    output_root: Path,
    dpi: int,
    timeout_seconds: float = 10,
) -> RenderedDrawing:
    started = perf_counter()
    source = pdf_path.read_bytes()
    drawing_sha256 = sha256(source).hexdigest()
    output_dir = output_root / drawing_sha256[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / "page"
    existing_pages = _rendered_pages(output_dir)
    if existing_pages:
        return RenderedDrawing(
            drawing_sha256=drawing_sha256,
            pages=existing_pages,
            duration_seconds=perf_counter() - started,
            cache_hit=True,
        )

    process = await asyncio.create_subprocess_exec(
        "pdftoppm",
        "-png",
        "-r",
        str(dpi),
        str(pdf_path),
        str(prefix),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise DrawingRenderError(f"PDF 渲染超过 {timeout_seconds:.1f} 秒") from error

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise DrawingRenderError(f"pdftoppm 执行失败：{message}")

    pages = _rendered_pages(output_dir)
    if not pages:
        raise DrawingRenderError("PDF 渲染未生成 PNG 页面")

    return RenderedDrawing(
        drawing_sha256=drawing_sha256,
        pages=pages,
        duration_seconds=perf_counter() - started,
        cache_hit=False,
    )
