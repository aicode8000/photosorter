import argparse
import io
import logging
import os
import queue
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import photosorter  # noqa: E402


def make_logger() -> logging.Logger:
    logger = logging.getLogger("photosorter_test")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.NullHandler())
    return logger


class MessageCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class FakeSession:
    def __init__(self, date_taken: str | None):
        self.date_taken = date_taken

    def get_date_taken(self, _file_path: str) -> str | None:
        return self.date_taken


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.returncode = returncode
        self.terminate_called = False
        self.kill_called = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise photosorter.subprocess.TimeoutExpired(cmd="exiftool", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = 1

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = 1


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


def test_exiftool_session_reads_until_ready_marker():
    logger = make_logger()
    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = logger
    session.command_timeout = 0.1
    session.process = FakeProcess()
    session.stdout_queue = queue.Queue()
    session.stdout_queue.put('[{"DateTimeOriginal":"2020:05:01 10:11:12"}]\n')
    session.stdout_queue.put(f"{photosorter.READY_MARKER}\n")

    assert session._read_response("/tmp/file.jpg") == '[{"DateTimeOriginal":"2020:05:01 10:11:12"}]\n'


def test_exiftool_session_times_out_and_terminates_process():
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)

    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = logger
    session.command_timeout = 0.01
    session.process = FakeProcess()
    session.stdout_queue = queue.Queue()

    assert session._read_response("/tmp/stuck.jpg") == ""
    assert session.process.terminate_called is True
    assert "timed out" in collector.messages[-1]


def test_exiftool_session_logs_stderr_lines():
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)

    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = logger
    session.process = FakeProcess()
    session.process.stderr = io.StringIO("Warning 1\nWarning 2\n")

    session._drain_stderr()

    assert collector.messages == ["Exiftool stderr: Warning 1", "Exiftool stderr: Warning 2"]


def test_exiftool_session_restarts_dead_process(monkeypatch):
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)

    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = logger
    session.command_timeout = 0.1
    session.process = FakeProcess(returncode=1)

    started: list[bool] = []
    written: list[str] = []

    def fake_start_process() -> None:
        started.append(True)
        session.process = FakeProcess()
        session.stdout_queue = queue.Queue()
        session.stdout_queue.put(f"{photosorter.READY_MARKER}\n")

    monkeypatch.setattr(session, "_start_process", fake_start_process)
    monkeypatch.setattr(session, "_write_lines", lambda lines: written.extend(lines))

    assert session.execute(["-j", "/tmp/file.jpg"]) == ""
    assert started == [True]
    assert written == ["-j", "/tmp/file.jpg", "-execute"]
    assert "restarting session" in collector.messages[0]


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


def test_move_or_copy_video_uses_year_videos(tmp_path):
    logger = make_logger()
    source = tmp_path / "clip.mp4"
    source.write_text("data")
    timestamp = datetime(2021, 6, 7, 8, 9, 10).timestamp()
    os.utime(source, (timestamp, timestamp))

    session = FakeSession(None)
    destination_root = tmp_path / "dest"
    photosorter.move_or_copy_file(
        str(source),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
        session=session,
    )

    expected = destination_root / "2021" / "Videos" / "clip.mp4"
    assert source.exists()
    assert expected.exists()


def test_move_or_copy_skips_missing_file(tmp_path):
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)
    destination_root = tmp_path / "dest"

    photosorter.move_or_copy_file(
        str(tmp_path / "missing.mp4"),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
        session=FakeSession(None),
    )

    assert not destination_root.exists()
    assert "Skipping missing or unsupported file" in collector.messages[0]


def test_move_or_copy_skips_file_that_disappears_during_processing(monkeypatch, tmp_path):
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)

    source = tmp_path / "clip.mp4"
    source.write_text("data")

    def missing_file(_file_path: str) -> float:
        raise FileNotFoundError

    monkeypatch.setattr(photosorter.os.path, "getmtime", missing_file)

    photosorter.move_or_copy_file(
        str(source),
        str(tmp_path / "dest"),
        logger,
        dry_run=False,
        copy_files=True,
        session=FakeSession(None),
    )

    assert "Skipping file that disappeared during processing" in collector.messages[-1]


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
