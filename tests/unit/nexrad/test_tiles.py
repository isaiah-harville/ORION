import io

import numpy as np
from PIL import Image

from services.nexrad.mosaic import GridSpec, create_grid
from services.nexrad.tiles import (
    colorize_dbz,
    dbz_palette_indexes,
    encode_tile,
    render_tile_pyramid,
)


def test_a_paletted_tile_decodes_to_the_rgba_colorize_produces() -> None:
    dbz = np.linspace(-10, 80, 256 * 256, dtype=np.float32).reshape(256, 256)
    dbz[:10] = np.nan
    png = encode_tile(dbz_palette_indexes(dbz))
    decoded = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert Image.open(io.BytesIO(png)).mode == "P"
    assert np.array_equal(decoded, colorize_dbz(dbz))


def _storm_grid():
    grid = create_grid(GridSpec(-90.0, 34.0, -86.0, 37.0, resolution_m=2000))
    rows, columns = grid.dbz.shape
    grid.dbz[rows // 3 : rows // 2, columns // 4 : columns // 2] = 45.0
    grid.dbz[rows // 2 : rows // 2 + 20, columns // 2 : columns // 2 + 30] = 25.0
    return grid


def test_parallel_rendering_writes_the_same_tiles(tmp_path) -> None:
    grid = _storm_grid()
    serial = render_tile_pyramid(grid, tmp_path / "serial", 4, 8, workers=1)
    parallel = render_tile_pyramid(grid, tmp_path / "parallel", 4, 8, workers=4)
    assert serial == parallel > 0
    files = sorted(
        p.relative_to(tmp_path / "serial") for p in (tmp_path / "serial").rglob("*.png")
    )
    assert files == sorted(
        p.relative_to(tmp_path / "parallel")
        for p in (tmp_path / "parallel").rglob("*.png")
    )
    for name in files:
        assert (tmp_path / "serial" / name).read_bytes() == (
            tmp_path / "parallel" / name
        ).read_bytes()
