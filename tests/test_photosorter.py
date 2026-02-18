import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import photosorter  # noqa: E402


def make_logger() -> logging.Logger:
    logger = logging.getLogger("photosorter_test")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.NullHandler())
    return logger


class FakeSession:
    def __init__(self, date_taken: str | None):
        self.date_taken = date_taken

    def get_date_taken(self, _file_path: str) -> str | None:
        return self.date_taken


def test_extract_date_parses_datetimeoriginal():
    logger = make_logger()
    output = '[{"DateTimeOriginal":"2020:05:01 10:11:12"}]'
    assert (
        photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg")
        == "2020:05:01 10:11:12"
    )


def test_extract_date_fallback_createdate():
    logger = make_logger()
    output = '[{"CreateDate":"2019:04:03 02:01:00"}]'
    assert (
        photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg")
        == "2019:04:03 02:01:00"
    )


def test_extract_date_fallback_datecreated():
    logger = make_logger()
    output = '[{"DateCreated":"2018:07:09 12:13:14"}]'
    assert (
        photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg")
        == "2018:07:09 12:13:14"
    )


def test_extract_date_invalid_json():
    logger = make_logger()
    assert photosorter.extract_date_from_json("not json", logger, "/tmp/file.jpg") is None


def test_move_or_copy_file_dry_run(tmp_path):
    logger = make_logger()
    source = tmp_path / "photo.jpg"
    source.write_text("data")

    session = FakeSession("2020:01:02 03:04:05")
    destination_root = tmp_path / "dest"
    photosorter.move_or_copy_file(
        str(source),
        str(destination_root),
        logger,
        dry_run=True,
        copy_files=False,
        session=session,
    )

    assert source.exists()
    assert not destination_root.exists()


def test_move_or_copy_file_copy(tmp_path):
    logger = make_logger()
    source = tmp_path / "photo.jpg"
    source.write_text("data")

    session = FakeSession("2020:01:02 03:04:05")
    destination_root = tmp_path / "dest"
    photosorter.move_or_copy_file(
        str(source),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
        session=session,
    )

    expected = destination_root / "2020" / "01" / "JPG" / "photo.jpg"
    assert source.exists()
    assert expected.exists()


def test_main_uses_single_mocked_session(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "dest"
    source_dir.mkdir()
    (source_dir / "photo.jpg").write_text("data")

    class StubSession:
        entered = False
        exited = False
        get_calls = 0

        def __init__(self, _logger):
            pass

        def __enter__(self):
            StubSession.entered = True
            return self

        def __exit__(self, exc_type, exc, exc_tb):
            StubSession.exited = True

        def get_date_taken(self, _file_path: str):
            StubSession.get_calls += 1
            return "2020:01:02 03:04:05"

    monkeypatch.setattr(photosorter, "ExifToolSession", StubSession)
    monkeypatch.setattr(photosorter.shutil, "which", lambda _cmd: "/usr/bin/exiftool")
    monkeypatch.setattr(
        photosorter,
        "parse_args",
        lambda: argparse.Namespace(
            source_directory=str(source_dir),
            destination_directory=str(destination_dir),
            dry_run=False,
            copy=False,
            log_file=None,
        ),
    )

    assert photosorter.main() == 0
    assert StubSession.entered is True
    assert StubSession.exited is True
    assert StubSession.get_calls == 1
    assert (destination_dir / "2020" / "01" / "JPG" / "photo.jpg").exists()
