import logging
import subprocess
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


def test_get_date_taken_parses_json(monkeypatch):
    logger = make_logger()
    stdout = '[{"DateTimeOriginal":"2020:05:01 10:11:12"}]'
    result = subprocess.CompletedProcess(args=["exiftool"], returncode=0, stdout=stdout, stderr="")

    def fake_run(*_args, **_kwargs):
        return result

    monkeypatch.setattr(photosorter.subprocess, "run", fake_run)

    assert photosorter.get_date_taken("/tmp/file.jpg", logger) == "2020:05:01 10:11:12"


def test_get_date_taken_invalid_json(monkeypatch, caplog):
    logger = make_logger()
    result = subprocess.CompletedProcess(
        args=["exiftool"],
        returncode=0,
        stdout="not json",
        stderr="",
    )

    def fake_run(*_args, **_kwargs):
        return result

    monkeypatch.setattr(photosorter.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING):
        assert photosorter.get_date_taken("/tmp/file.jpg", logger) is None


def test_get_date_taken_exiftool_failure(monkeypatch):
    logger = make_logger()

    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=["exiftool"], stderr="boom")

    monkeypatch.setattr(photosorter.subprocess, "run", fake_run)

    assert photosorter.get_date_taken("/tmp/file.jpg", logger) is None


def test_move_or_copy_file_dry_run(tmp_path, monkeypatch):
    logger = make_logger()
    source = tmp_path / "photo.jpg"
    source.write_text("data")

    def fake_get_date_taken(*_args, **_kwargs):
        return "2020:01:02 03:04:05"

    monkeypatch.setattr(photosorter, "get_date_taken", fake_get_date_taken)

    destination_root = tmp_path / "dest"
    photosorter.move_or_copy_file(
        str(source),
        str(destination_root),
        logger,
        dry_run=True,
        copy_files=False,
    )

    assert source.exists()
    assert not destination_root.exists()

def test_move_or_copy_file_copy(tmp_path, monkeypatch):
    logger = make_logger()
    source = tmp_path / "photo.jpg"
    source.write_text("data")

    def fake_get_date_taken(*_args, **_kwargs):
        return "2020:01:02 03:04:05"

    monkeypatch.setattr(photosorter, "get_date_taken", fake_get_date_taken)

    destination_root = tmp_path / "dest"
    photosorter.move_or_copy_file(
        str(source),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
    )

    expected = destination_root / "2020" / "01" / "JPG" / "photo.jpg"
    assert source.exists()
    assert expected.exists()
