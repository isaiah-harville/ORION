import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.nexrad import ingest as ingest_module
from services.nexrad.archive import ScanObject
from services.nexrad.ingest import download_and_extract
from services.nexrad.retention import prune_raw_scans, prune_superseded_frames


def scan(root: Path, station: str, name: str, age_seconds: float) -> Path:
    path = root / station / "20260830" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"volume")
    stamp = time.time() - age_seconds
    import os

    os.utime(path, (stamp, stamp))
    return path


def fake_ingest(root: Path, monkeypatch, *, decode_fails: bool = False):
    """Stand in for the S3 download and pyart decode around the delete."""
    downloaded = root / "KOHX" / "20260830" / "KOHX20260830_000000_V06"

    def download(_client, _bucket, _scan, _cache, first_sweep_only=False):
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"volume")
        return downloaded

    def extract(_path, _station, _range):
        if decode_fails:
            raise ValueError("corrupt volume")
        return "gate-scan"

    monkeypatch.setattr(ingest_module, "download_scan", download)
    monkeypatch.setattr(ingest_module, "extract_lowest_reflectivity", extract)
    return downloaded


SCAN = ScanObject("KOHX", datetime(2026, 8, 30, tzinfo=UTC), "key", 6)


def test_volume_is_deleted_once_decoded(make_settings, monkeypatch) -> None:
    config = make_settings()
    path = fake_ingest(config.raw_directory, monkeypatch)
    assert download_and_extract(None, config, SCAN) == "gate-scan"
    assert not path.exists()


def test_volume_is_deleted_even_when_decoding_fails(make_settings, monkeypatch) -> None:
    config = make_settings()
    path = fake_ingest(config.raw_directory, monkeypatch, decode_fails=True)
    with pytest.raises(ValueError):
        download_and_extract(None, config, SCAN)
    # A corrupt volume must not be the one file that survives every cycle.
    assert not path.exists()


def test_volume_is_kept_when_configured(make_settings, monkeypatch) -> None:
    config = make_settings()
    object.__setattr__(config, "keep_raw_scans", True)
    path = fake_ingest(config.raw_directory, monkeypatch)
    download_and_extract(None, config, SCAN)
    assert path.is_file()


def test_prune_removes_only_scans_past_retention(make_settings) -> None:
    config = make_settings()
    fresh = scan(config.raw_directory, "KOHX", "fresh_V06", age_seconds=60)
    stale = scan(config.raw_directory, "KHTX", "stale_V06", age_seconds=7200)

    prune_raw_scans(config)

    assert fresh.is_file()
    assert not stale.exists()
    # The cache root must survive so the next download does not have to recreate it.
    assert config.raw_directory.is_dir()


def test_prune_is_disabled_by_zero_retention(make_settings) -> None:
    config = make_settings(raw_retention=timedelta(0))
    stale = scan(config.raw_directory, "KOHX", "stale_V06", age_seconds=7200)
    prune_raw_scans(config)
    assert stale.is_file()


def test_superseded_frames_are_removed_once_aged(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    current = frames / "1788000000-aaaaaaaa"
    old = frames / "1788000000-bbbbbbbb"
    recent_other = frames / "1788000001-cccccccc"
    for directory in (current, old, recent_other):
        directory.mkdir()
        (directory / "metadata.json").write_text("{}")

    import os

    stamp = time.time() - 7200
    os.utime(old, (stamp, stamp))

    prune_superseded_frames(frames, "-aaaaaaaa")

    assert current.is_dir()
    assert not old.exists()
    # A concurrent producer's fresh output is left alone by the age guard.
    assert recent_other.is_dir()


def test_manifest_keeps_frames_by_age_not_count(make_settings) -> None:
    import json
    from datetime import timedelta

    from services.nexrad.manifest import publish_manifest
    from services.nexrad.settings import configuration_hash

    settings = make_settings(history=timedelta(hours=1), retained_frames=100)
    frames = settings.mosaic_directory / "frames"
    suffix = configuration_hash(settings)
    # An idle stretch: 15-minute frames, then the five-minute cadence resumes.
    times = [0, 900, 1800, 2700, 3600, 3900, 4200]
    for t in times:
        directory = frames / f"{1_800_000_000 + t}-{suffix}"
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "id": directory.name,
                    "time": 1_800_000_000 + t,
                    "stations": [],
                    "max_skew_seconds": 0,
                }
            )
        )

    publish_manifest(settings)

    manifest = json.loads((settings.mosaic_directory / "manifest.json").read_text())
    kept = [frame["time"] - 1_800_000_000 for frame in manifest["frames"]]
    assert kept == [900, 1800, 2700, 3600, 3900, 4200]
    assert not (frames / f"{1_800_000_000}-{suffix}").exists()
