from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mercantile
import numpy as np
from PIL import Image
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import Resampling, reproject

from services.nexrad.mosaic import MosaicGrid

DBZ_STOPS = np.asarray([20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70])
# Alpha climbs with intensity so the map reads by weight as well as hue. The
# lowest band is deliberately faint: it is the noisiest part of the scale, and a
# neon 20 dBZ speck otherwise draws the eye harder than a 30 dBZ shower.
DBZ_COLORS = np.asarray(
    [
        [2, 253, 2, 105],
        [1, 197, 1, 140],
        [0, 142, 0, 170],
        [253, 248, 2, 195],
        [229, 188, 0, 210],
        [253, 149, 0, 225],
        [253, 0, 0, 235],
        [212, 0, 0, 240],
        [188, 0, 0, 245],
        [248, 0, 253, 250],
        [152, 84, 198, 255],
    ],
    dtype=np.uint8,
)


# Tiles hold only the colours above plus transparent, so they are written as
# paletted PNGs: index 0 is fully transparent, 1..11 are DBZ_COLORS. A browser
# decodes them to exactly the RGBA an RGBA PNG would carry, at a quarter of the
# bytes to compress; encoding is ~9x faster and tiles ~35% smaller.
PALETTE = [0, 0, 0, *DBZ_COLORS[:, :3].flatten().tolist()]
PALETTE_ALPHA = bytes([0, *DBZ_COLORS[:, 3].tolist()])
# zlib level 6 rather than optimize=True (level 9 plus extra passes): tiles are
# rendered every cycle but each is fetched rarely, so encode time dominates.
PNG_COMPRESS_LEVEL = 6


def dbz_palette_indexes(dbz: np.ndarray) -> np.ndarray:
    """Palette index per pixel: 0 for no echo, 1..11 for the DBZ_COLORS bands."""
    indexes = np.zeros(dbz.shape, dtype=np.uint8)
    valid = np.isfinite(dbz) & (dbz >= DBZ_STOPS[0])
    bands = np.searchsorted(DBZ_STOPS, dbz[valid], side="right") - 1
    indexes[valid] = np.clip(bands, 0, len(DBZ_COLORS) - 1) + 1
    return indexes


def colorize_dbz(dbz: np.ndarray) -> np.ndarray:
    """RGBA for a dBZ array; what a paletted tile decodes to."""
    lookup = np.vstack((np.zeros((1, 4), dtype=np.uint8), DBZ_COLORS))
    return lookup[dbz_palette_indexes(dbz)]


def encode_tile(indexes: np.ndarray) -> bytes:
    image = Image.fromarray(indexes, mode="P")
    image.putpalette(PALETTE)
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="PNG",
        transparency=PALETTE_ALPHA,
        compress_level=PNG_COMPRESS_LEVEL,
    )
    return buffer.getvalue()


def _reproject_tile(grid: MosaicGrid, tile: mercantile.Tile) -> np.ndarray:
    bounds = mercantile.xy_bounds(tile)
    destination = np.full((256, 256), np.nan, dtype=np.float32)
    reproject(
        source=grid.dbz,
        destination=destination,
        src_transform=from_origin(
            grid.min_x, grid.max_y, grid.resolution_m, grid.resolution_m
        ),
        src_crs="EPSG:3857",
        src_nodata=np.nan,
        dst_transform=from_bounds(
            bounds.left, bounds.bottom, bounds.right, bounds.top, 256, 256
        ),
        dst_crs="EPSG:3857",
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return destination


def render_tile(grid: MosaicGrid, tile: mercantile.Tile) -> np.ndarray:
    return colorize_dbz(_reproject_tile(grid, tile))


def _tile_png(grid: MosaicGrid, tile: mercantile.Tile) -> bytes | None:
    """The encoded tile, or None when reprojection leaves it empty."""
    indexes = dbz_palette_indexes(_reproject_tile(grid, tile))
    if not indexes.any():
        return None
    return encode_tile(indexes)


def _tile_has_visible_echo(grid: MosaicGrid, tile: mercantile.Tile) -> bool:
    bounds = mercantile.xy_bounds(tile)
    col_start = max(0, int((bounds.left - grid.min_x) / grid.resolution_m) - 1)
    col_end = min(
        grid.dbz.shape[1],
        int(np.ceil((bounds.right - grid.min_x) / grid.resolution_m)) + 1,
    )
    row_start = max(0, int((grid.max_y - bounds.top) / grid.resolution_m) - 1)
    row_end = min(
        grid.dbz.shape[0],
        int(np.ceil((grid.max_y - bounds.bottom) / grid.resolution_m)) + 1,
    )
    if row_start >= row_end or col_start >= col_end:
        return False
    window = grid.dbz[row_start:row_end, col_start:col_end]
    return bool(np.any(window >= DBZ_STOPS[0]))


def render_tile_pyramid(
    grid: MosaicGrid,
    frame_directory: Path,
    minimum_zoom: int,
    maximum_zoom: int,
    workers: int = 1,
) -> int:
    """Render every non-empty tile of the pyramid, returning how many.

    Tiles are independent, and GDAL's reprojection and zlib both release the
    GIL, so they render in parallel across `workers` threads.
    """
    # A continental pyramid is mostly empty tiles; checking the source window
    # is far cheaper than reprojecting each one.
    tiles = [
        tile
        for tile in mercantile.tiles(
            grid.west,
            grid.south,
            grid.east,
            grid.north,
            zooms=range(minimum_zoom, maximum_zoom + 1),
        )
        if _tile_has_visible_echo(grid, tile)
    ]

    def render(tile: mercantile.Tile) -> bool:
        png = _tile_png(grid, tile)
        if png is None:
            return False
        path = frame_directory / str(tile.z) / str(tile.x) / f"{tile.y}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return True

    if workers <= 1:
        return sum(render(tile) for tile in tiles)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return sum(executor.map(render, tiles))
