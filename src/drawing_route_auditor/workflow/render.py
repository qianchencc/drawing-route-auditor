from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from drawing_route_auditor.workflow.models import RenderedDrawing


class DrawingRenderError(RuntimeError):
    pass


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
    existing_pages = tuple(sorted(output_dir.glob("page-*.png")))
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
        raise DrawingRenderError(
            f"PDF rendering exceeded {timeout_seconds:.1f}s"
        ) from error

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise DrawingRenderError(f"pdftoppm failed: {message}")

    pages = tuple(sorted(output_dir.glob("page-*.png")))
    if not pages:
        raise DrawingRenderError("PDF rendering produced no PNG pages")

    return RenderedDrawing(
        drawing_sha256=drawing_sha256,
        pages=pages,
        duration_seconds=perf_counter() - started,
        cache_hit=False,
    )
