import argparse
import io
import logging
import os
import queue
import sys
from datetime import datetime
from pathlib import Path

import pytest

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
    def __init__(self, date_taken: datetime | None):
        self.date_taken = date_taken

    def get_date_taken(self, _file_path: str) -> datetime | None:
        return self.date_taken


class FailingSession:
    def get_date_taken(self, file_path: str) -> datetime | None:
        raise photosorter.ExifToolError(f"exiftool timed out while processing {file_path}")


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
        == datetime(2020, 5, 1, 10, 11, 12)
    )


def test_extract_date_fallback_createdate():
    logger = make_logger()
    output = '[{"CreateDate":"2019:04:03 02:01:00"}]'
    assert (
        photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg")
        == datetime(2019, 4, 3, 2, 1, 0)
    )


def test_extract_date_fallback_datecreated():
    logger = make_logger()
    output = '[{"DateCreated":"2018:07:09 12:13:14"}]'
    assert (
        photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg")
        == datetime(2018, 7, 9, 12, 13, 14)
    )


def test_extract_date_invalid_json():
    logger = make_logger()
    assert photosorter.extract_date_from_json("not json", logger, "/tmp/file.jpg") is None


def test_extract_date_skips_zero_date_and_falls_back():
    logger = make_logger()
    output = '[{"DateTimeOriginal":"0000:00:00 00:00:00","CreateDate":"2019:03:01 08:00:00"}]'
    assert photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg") == datetime(
        2019, 3, 1, 8, 0, 0
    )


def test_extract_date_returns_none_when_all_dates_invalid():
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)
    output = '[{"DateTimeOriginal":"0000:00:00 00:00:00","CreateDate":"    :  :     :  :  "}]'

    assert photosorter.extract_date_from_json(output, logger, "/tmp/file.jpg") is None
    assert "No valid date" in collector.messages[-1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2023:05:12 14:33:10", datetime(2023, 5, 12, 14, 33, 10)),
        ("2023:05:12 14:33:10+02:00", datetime(2023, 5, 12, 14, 33, 10)),
        ("2023:05:12 14:33:10.123", datetime(2023, 5, 12, 14, 33, 10)),
        ("2023:05:12 14:33:10.123Z", datetime(2023, 5, 12, 14, 33, 10)),
        ("2023:05:12", datetime(2023, 5, 12)),
        ("2023-05-12T14:33:10", datetime(2023, 5, 12, 14, 33, 10)),
        ("0000:00:00 00:00:00", None),
        ("2023:13:01 00:00:00", None),
        ("garbage", None),
        (20230512, None),
    ],
)
def test_parse_exif_date(value, expected):
    assert photosorter.parse_exif_date(value) == expected


def test_exiftool_session_reads_until_ready_marker():
    logger = make_logger()
    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = logger
    session.command_timeout = 0.1
    session.process = FakeProcess()
    session.stdout_queue = queue.Queue()
    session.stdout_queue.put('[{"DateTimeOriginal":"2020:05:01 10:11:12"}]\n')
    session.stdout_queue.put(f"{photosorter.READY_MARKER}\n")

    expected = '[{"DateTimeOriginal":"2020:05:01 10:11:12"}]\n'
    assert session._read_response("/tmp/file.jpg") == expected


def test_exiftool_session_times_out_and_terminates_process():
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)

    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = logger
    session.command_timeout = 0.01
    session.process = FakeProcess()
    session.stdout_queue = queue.Queue()

    with pytest.raises(photosorter.ExifToolError, match="timed out"):
        session._read_response("/tmp/stuck.jpg")
    assert session.process.terminate_called is True


def test_exiftool_session_raises_when_process_exits():
    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = make_logger()
    session.command_timeout = 0.1
    session.process = FakeProcess(returncode=1)
    session.stdout_queue = queue.Queue()
    session.stdout_queue.put(None)

    with pytest.raises(photosorter.ExifToolError, match="exited unexpectedly"):
        session._read_response("/tmp/file.jpg")


def test_exiftool_session_raises_when_restart_fails(monkeypatch):
    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = make_logger()
    session.command_timeout = 0.1
    session.process = FakeProcess(returncode=1)

    def failing_start() -> None:
        raise OSError("exiftool not found")

    monkeypatch.setattr(session, "_start_process", failing_start)

    with pytest.raises(photosorter.ExifToolError, match="failed to restart"):
        session.execute(["-j", "/tmp/file.jpg"])


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


def test_process_group_file_dry_run(tmp_path):
    logger = make_logger()
    source = tmp_path / "photo.jpg"
    source.write_text("data")

    session = FakeSession(datetime(2020, 1, 2, 3, 4, 5))
    destination_root = tmp_path / "dest"
    photosorter.process_group(
        photosorter.MediaGroup(str(source)),
        str(destination_root),
        logger,
        dry_run=True,
        copy_files=False,
        session=session,
    )

    assert source.exists()
    assert not destination_root.exists()


def test_process_group_file_copy(tmp_path):
    logger = make_logger()
    source = tmp_path / "photo.jpg"
    source.write_text("data")

    session = FakeSession(datetime(2020, 1, 2, 3, 4, 5))
    destination_root = tmp_path / "dest"
    photosorter.process_group(
        photosorter.MediaGroup(str(source)),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
        session=session,
    )

    expected = destination_root / "2020" / "01" / "JPG" / "photo.jpg"
    assert source.exists()
    assert expected.exists()


def test_process_group_video_uses_year_videos(tmp_path):
    logger = make_logger()
    source = tmp_path / "clip.mp4"
    source.write_text("data")
    timestamp = datetime(2021, 6, 7, 8, 9, 10).timestamp()
    os.utime(source, (timestamp, timestamp))

    session = FakeSession(None)
    destination_root = tmp_path / "dest"
    photosorter.process_group(
        photosorter.MediaGroup(str(source)),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
        session=session,
    )

    expected = destination_root / "2021" / "Videos" / "clip.mp4"
    assert source.exists()
    assert expected.exists()


@pytest.mark.parametrize("file_name", ["photo.jpg", "clip.mp4"])
def test_process_group_leaves_file_in_place_when_exiftool_fails(tmp_path, file_name):
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)
    source = tmp_path / file_name
    source.write_text("data")
    destination_root = tmp_path / "dest"

    photosorter.process_group(
        photosorter.MediaGroup(str(source)),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=False,
        session=FailingSession(),
    )

    assert source.exists()
    assert not destination_root.exists()
    assert "exiftool failed" in collector.messages[-1]


def test_process_group_skips_missing_file(tmp_path):
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)
    destination_root = tmp_path / "dest"

    photosorter.process_group(
        photosorter.MediaGroup(str(tmp_path / "missing.mp4")),
        str(destination_root),
        logger,
        dry_run=False,
        copy_files=True,
        session=FakeSession(None),
    )

    assert not destination_root.exists()
    assert "Skipping missing or unsupported file" in collector.messages[0]


def test_process_group_skips_file_that_disappears_during_processing(monkeypatch, tmp_path):
    logger = make_logger()
    collector = MessageCollector()
    logger.addHandler(collector)

    source = tmp_path / "clip.mp4"
    source.write_text("data")

    def missing_file(_file_path: str) -> float:
        raise FileNotFoundError

    monkeypatch.setattr(photosorter.os.path, "getmtime", missing_file)

    photosorter.process_group(
        photosorter.MediaGroup(str(source)),
        str(tmp_path / "dest"),
        logger,
        dry_run=False,
        copy_files=True,
        session=FakeSession(None),
    )

    assert "Skipping file that disappeared during processing" in collector.messages[-1]


def make_stub_session():
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
            return datetime(2020, 1, 2, 3, 4, 5)

    return StubSession


def run_main(monkeypatch, source_dir, destination_dir, dry_run=False, stub=None):
    stub = stub or make_stub_session()
    monkeypatch.setattr(photosorter, "ExifToolSession", stub)
    monkeypatch.setattr(photosorter.shutil, "which", lambda _cmd: "/usr/bin/exiftool")
    monkeypatch.setattr(
        photosorter,
        "parse_args",
        lambda: argparse.Namespace(
            source_directory=str(source_dir),
            destination_directory=str(destination_dir),
            dry_run=dry_run,
            copy=False,
            log_file=None,
        ),
    )
    return photosorter.main(), stub


def test_main_uses_single_mocked_session(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "dest"
    source_dir.mkdir()
    (source_dir / "photo.jpg").write_text("data")

    exit_code, StubSession = run_main(monkeypatch, source_dir, destination_dir)

    assert exit_code == 0
    assert StubSession.entered is True
    assert StubSession.exited is True
    assert StubSession.get_calls == 1
    assert (destination_dir / "2020" / "01" / "JPG" / "photo.jpg").exists()


def test_main_dry_run_does_not_create_destination(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "dest"
    source_dir.mkdir()
    (source_dir / "photo.jpg").write_text("data")

    exit_code, _ = run_main(monkeypatch, source_dir, destination_dir, dry_run=True)

    assert exit_code == 0
    assert not destination_dir.exists()
    assert (source_dir / "photo.jpg").exists()


def test_main_rejects_same_source_and_destination(monkeypatch, tmp_path):
    source_dir = tmp_path / "photos"
    source_dir.mkdir()
    (source_dir / "photo.jpg").write_text("data")

    exit_code, stub = run_main(monkeypatch, source_dir, tmp_path / "photos" / ".." / "photos")

    assert exit_code == 1
    assert stub.get_calls == 0
    assert (source_dir / "photo.jpg").exists()


def test_main_rejects_missing_source(monkeypatch, tmp_path):
    exit_code, stub = run_main(monkeypatch, tmp_path / "missing", tmp_path / "dest")

    assert exit_code == 1
    assert stub.get_calls == 0


def test_main_does_not_resort_destination_inside_source(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    destination_dir = source_dir / "sorted"
    already_sorted = destination_dir / "2019" / "05" / "JPG" / "old.jpg"
    already_sorted.parent.mkdir(parents=True)
    already_sorted.write_text("old")
    (source_dir / "photo.jpg").write_text("data")

    exit_code, stub = run_main(monkeypatch, source_dir, destination_dir)

    assert exit_code == 0
    assert stub.get_calls == 1
    assert already_sorted.exists()
    assert (destination_dir / "2020" / "01" / "JPG" / "photo.jpg").exists()
    assert not (destination_dir / "2020" / "01" / "JPG" / "photo_1.jpg").exists()


# --- grouping of sidecars, Live Photos and non-media files ---------------------------------


def group_names(directory, names):
    groups, untouched = photosorter.group_directory_files(str(directory), names)
    as_names = {
        os.path.basename(g.main): sorted(os.path.basename(c) for c in g.companions)
        for g in groups
    }
    return as_names, sorted(os.path.basename(p) for p in untouched)


def test_group_attaches_sidecars_to_raw_of_raw_jpeg_pair(tmp_path):
    groups, untouched = group_names(
        tmp_path, ["DSC_0001.NEF", "DSC_0001.JPG", "DSC_0001.xmp", "DSC_0001.NEF.dop"]
    )
    assert groups == {"DSC_0001.JPG": [], "DSC_0001.NEF": ["DSC_0001.NEF.dop", "DSC_0001.xmp"]}
    assert untouched == []


def test_group_attaches_live_photo_video_and_aae_to_heic(tmp_path):
    groups, _ = group_names(tmp_path, ["IMG_0001.HEIC", "IMG_0001.MOV", "IMG_0001.AAE"])
    assert groups == {"IMG_0001.HEIC": ["IMG_0001.AAE", "IMG_0001.MOV"]}


def test_group_keeps_mov_standalone_when_only_a_raw_shares_its_name(tmp_path):
    groups, _ = group_names(tmp_path, ["DSC_0001.NEF", "DSC_0001.MOV"])
    assert groups == {"DSC_0001.MOV": [], "DSC_0001.NEF": []}


def test_group_leaves_non_media_and_orphan_sidecars_and_hides_junk(tmp_path):
    groups, untouched = group_names(
        tmp_path,
        ["photo.jpg", "notes.txt", "orphan.xmp", ".DS_Store", "._photo.jpg", "Thumbs.db"],
    )
    assert groups == {"photo.jpg": []}
    assert untouched == ["notes.txt", "orphan.xmp"]


@pytest.mark.parametrize(
    ("file_name", "folder"),
    [("a.jpeg", "JPG"), ("a.JPG", "JPG"), ("a.heif", "HEIC"), ("a.tif", "TIFF"), ("a.nef", "NEF")],
)
def test_extension_folder_merges_aliases(file_name, folder):
    assert photosorter.extension_folder(file_name) == folder


# --- process_group: companions, collisions, duplicates -------------------------------------


def run_group(group, destination_root, date=datetime(2020, 1, 2, 3, 4, 5), copy_files=False):
    return photosorter.process_group(
        group,
        str(destination_root),
        make_logger(),
        dry_run=False,
        copy_files=copy_files,
        session=FakeSession(date),
    )


def test_process_group_moves_companions_and_renames_them_on_collision(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for name in ["DSC_0001.NEF", "DSC_0001.xmp", "DSC_0001.NEF.dop"]:
        (source / name).write_text(f"new {name}")
    target_dir = tmp_path / "dest" / "2020" / "01" / "NEF"
    target_dir.mkdir(parents=True)
    (target_dir / "DSC_0001.NEF").write_text("another photo with the same name")

    group = photosorter.MediaGroup(
        str(source / "DSC_0001.NEF"),
        [str(source / "DSC_0001.xmp"), str(source / "DSC_0001.NEF.dop")],
    )

    assert run_group(group, tmp_path / "dest") == "sorted"
    assert (target_dir / "DSC_0001_1.NEF").read_text() == "new DSC_0001.NEF"
    assert (target_dir / "DSC_0001_1.xmp").read_text() == "new DSC_0001.xmp"
    assert (target_dir / "DSC_0001_1.NEF.dop").read_text() == "new DSC_0001.NEF.dop"
    assert list(source.iterdir()) == []


def test_process_group_puts_live_photo_video_next_to_photo(tmp_path):
    (tmp_path / "IMG_0001.HEIC").write_text("photo")
    (tmp_path / "IMG_0001.MOV").write_text("video")
    group = photosorter.MediaGroup(
        str(tmp_path / "IMG_0001.HEIC"), [str(tmp_path / "IMG_0001.MOV")]
    )

    assert run_group(group, tmp_path / "dest") == "sorted"
    assert (tmp_path / "dest" / "2020" / "01" / "HEIC" / "IMG_0001.MOV").exists()


@pytest.mark.parametrize("existing_name", ["photo.jpg", "photo_1.jpg"])
def test_process_group_leaves_duplicates_in_place(tmp_path, existing_name):
    source = tmp_path / "photo.jpg"
    source.write_text("same bytes")
    target_dir = tmp_path / "dest" / "2020" / "01" / "JPG"
    target_dir.mkdir(parents=True)
    if existing_name != "photo.jpg":
        (target_dir / "photo.jpg").write_text("different bytes")
    (target_dir / existing_name).write_text("same bytes")

    assert run_group(photosorter.MediaGroup(str(source)), tmp_path / "dest") == "duplicate"
    assert source.exists()
    assert sorted(p.name for p in target_dir.iterdir()) == sorted({"photo.jpg", existing_name})


def test_process_group_photo_without_date_is_unclassified(tmp_path):
    source = tmp_path / "screenshot.png"
    source.write_text("data")

    assert run_group(photosorter.MediaGroup(str(source)), tmp_path / "dest", date=None) == (
        "unclassified"
    )
    assert (tmp_path / "dest" / "Non_classee" / "screenshot.png").exists()


# --- exiftool request -----------------------------------------------------------------------


def test_get_date_taken_requests_quicktime_utc_and_creation_date(monkeypatch):
    session = photosorter.ExifToolSession.__new__(photosorter.ExifToolSession)
    session.logger = make_logger()
    sent: list[list[str]] = []

    def fake_execute(args):
        sent.append(args)
        return '[{"CreationDate":"2023:12:31 23:30:00+01:00","CreateDate":"2023:12:31 22:30:00"}]'

    monkeypatch.setattr(session, "execute", fake_execute)

    assert session.get_date_taken("/tmp/clip.mov") == datetime(2023, 12, 31, 23, 30, 0)
    assert sent[0][:3] == ["-j", "-api", "QuickTimeUTC"]
    assert "-CreationDate" in sent[0]


# --- main: walk, summary, exit code ---------------------------------------------------------


def test_main_leaves_non_media_and_skips_system_directories(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "dest"
    for directory in [".@__thumb", "@eaDir", "#recycle"]:
        (source_dir / directory).mkdir(parents=True)
        (source_dir / directory / "thumb.jpg").write_text("thumbnail")
    (source_dir / "photo.jpg").write_text("data")
    (source_dir / "notes.txt").write_text("text")
    (source_dir / ".DS_Store").write_text("junk")

    exit_code, stub = run_main(monkeypatch, source_dir, destination_dir)

    assert exit_code == 0
    assert stub.get_calls == 1
    assert (source_dir / "notes.txt").exists()
    assert (source_dir / ".DS_Store").exists()
    assert not (destination_dir / "Non_classee").exists()


def test_main_returns_error_code_and_logs_summary(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "photo.jpg").write_text("data")
    (source_dir / "notes.txt").write_text("text")

    stub = make_stub_session()
    stub.get_date_taken = FailingSession.get_date_taken
    collector = MessageCollector()
    logger = photosorter.setup_logger(None)
    logger.addHandler(collector)
    try:
        exit_code, _ = run_main(monkeypatch, source_dir, tmp_path / "dest", stub=stub)
    finally:
        logger.removeHandler(collector)

    assert exit_code == 1
    assert (source_dir / "photo.jpg").exists()
    summary = collector.messages[-1]
    assert summary.startswith("Summary: 0 sorted")
    assert "1 non-media files left in place, 1 errors" in summary
