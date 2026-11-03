import argparse
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from types import TracebackType
from typing import TextIO

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".nef",
    ".heic",
    ".heif",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mts", ".avi", ".mkv"}

LOGGER_NAME = "photosorter"
READY_MARKER = "{ready}"
DATE_TAGS = ("DateTimeOriginal", "CreateDate", "DateCreated")
# Accepts "YYYY:MM:DD HH:MM:SS" plus optional sub-seconds / timezone suffix, or a bare date.
EXIF_DATE_RE = re.compile(r"^\s*(\d{4})[:-](\d{2})[:-](\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?")


class ExifToolError(RuntimeError):
    """Exiftool itself failed (timeout, crash, I/O): the file's date is unknown, not absent."""


def setup_logger(log_file: str | None) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def is_image_file(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() in IMAGE_EXTENSIONS


def is_video_file(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() in VIDEO_EXTENSIONS


def parse_exif_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = EXIF_DATE_RE.match(value)
    if not match:
        return None
    parts = [int(part) if part is not None else 0 for part in match.groups()]
    try:
        return datetime(*parts)
    except ValueError:
        # e.g. "0000:00:00 00:00:00" written by cameras with an unset clock
        return None


def extract_date_from_json(
    output: str, logger: logging.Logger, file_path: str
) -> datetime | None:
    output = output.strip()
    if not output:
        logger.warning("Exiftool returned empty JSON for %s", file_path)
        return None

    try:
        exif_data = json.loads(output)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON from exiftool for %s: %s", file_path, exc)
        return None

    if not isinstance(exif_data, list) or not exif_data or not isinstance(exif_data[0], dict):
        logger.warning("Unexpected JSON structure from exiftool for %s", file_path)
        return None

    invalid_values = []
    for tag in DATE_TAGS:
        value = exif_data[0].get(tag)
        if value is None:
            continue
        parsed = parse_exif_date(value)
        if parsed is not None:
            return parsed
        invalid_values.append(f"{tag}={value!r}")

    if invalid_values:
        logger.warning("No valid date for %s (%s)", file_path, ", ".join(invalid_values))
    return None


class ExifToolSession:
    def __init__(self, logger: logging.Logger, command_timeout: float = 30.0) -> None:
        self.logger = logger
        self.command_timeout = command_timeout
        self.process: subprocess.Popen[str]
        self.stdout_queue: queue.Queue[str | None]
        self._start_process()

    def _start_process(self) -> None:
        self.process = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.stdout_queue = queue.Queue()
        threading.Thread(
            target=self._drain_stdout,
            args=(self.process.stdout, self.stdout_queue),
            name="exiftool-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._drain_stderr,
            args=(self.process.stderr,),
            name="exiftool-stderr",
            daemon=True,
        ).start()

    def _drain_stdout(
        self,
        stdout: TextIO | None = None,
        stdout_queue: queue.Queue[str | None] | None = None,
    ) -> None:
        stdout = stdout or self.process.stdout
        stdout_queue = stdout_queue or self.stdout_queue
        if not stdout:
            return
        for line in stdout:
            stdout_queue.put(line)
        stdout_queue.put(None)

    def _drain_stderr(self, stderr: TextIO | None = None) -> None:
        stderr = stderr or self.process.stderr
        if not stderr:
            return
        for line in stderr:
            message = line.strip()
            if message:
                self.logger.warning("Exiftool stderr: %s", message)

    def _write_lines(self, lines: list[str]) -> None:
        if not self.process.stdin:
            raise ExifToolError("exiftool stdin is not available")
        for line in lines:
            self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def _terminate_process(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _read_response(self, request_target: str) -> str:
        response_lines: list[str] = []
        deadline = time.monotonic() + self.command_timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = self.stdout_queue.get(timeout=remaining)
            except queue.Empty:
                self._terminate_process()
                raise ExifToolError(
                    f"exiftool timed out after {self.command_timeout:.1f} seconds "
                    f"while processing {request_target}"
                ) from None

            if line is None:
                if self.process.poll() is None:
                    try:
                        self.process.wait(timeout=min(1.0, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                raise ExifToolError(
                    f"exiftool exited unexpectedly while processing {request_target}"
                )
            if line.strip() == READY_MARKER:
                break
            response_lines.append(line)
        return "".join(response_lines)

    def execute(self, args: list[str]) -> str:
        """Run one exiftool command. Raises ExifToolError if exiftool itself fails."""
        if self.process.poll() is not None:
            self.logger.warning("Exiftool is not running, restarting session")
            try:
                self._start_process()
            except OSError as exc:
                raise ExifToolError(f"failed to restart exiftool: {exc}") from exc
        try:
            self._write_lines(args + ["-execute"])
        except OSError as exc:
            self._terminate_process()
            raise ExifToolError(f"failed to communicate with exiftool: {exc}") from exc
        request_target = args[-1] if args else "request"
        return self._read_response(request_target)

    def get_date_taken(self, file_path: str) -> datetime | None:
        output = self.execute(["-j", *(f"-{tag}" for tag in DATE_TAGS), file_path])
        return extract_date_from_json(output, self.logger, file_path)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self._write_lines(["-stay_open", "False", "-execute"])
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_process()

    def __enter__(self) -> "ExifToolSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


def create_unique_file_name(file_path: str) -> str:
    counter = 1
    name, extension = os.path.splitext(file_path)
    while os.path.exists(f"{name}_{counter}{extension}"):
        counter += 1
    return f"{name}_{counter}{extension}"


def resolve_destination(
    file_path: str,
    destination_directory: str,
    logger: logging.Logger,
    session: ExifToolSession,
) -> str:
    if is_image_file(file_path):
        date_taken = session.get_date_taken(file_path)
        if date_taken is not None:
            file_extension = os.path.splitext(file_path)[1][1:].upper()
            return os.path.join(
                destination_directory,
                str(date_taken.year),
                f"{date_taken.month:02d}",
                file_extension,
            )
        return os.path.join(destination_directory, "Non_classee")
    if is_video_file(file_path):
        date_taken = session.get_date_taken(file_path)
        if date_taken is not None:
            return os.path.join(destination_directory, str(date_taken.year), "Videos")

        modified_year = datetime.fromtimestamp(os.path.getmtime(file_path)).year
        return os.path.join(destination_directory, str(modified_year), "Videos")
    return os.path.join(destination_directory, "Non_classee")


def move_or_copy_file(
    file_path: str,
    destination_directory: str,
    logger: logging.Logger,
    dry_run: bool,
    copy_files: bool,
    session: ExifToolSession,
) -> None:
    if not os.path.isfile(file_path):
        logger.warning("Skipping missing or unsupported file: %s", file_path)
        return

    operation = "copy" if copy_files else "move"
    try:
        destination_path = resolve_destination(file_path, destination_directory, logger, session)
        destination_file = os.path.join(destination_path, os.path.basename(file_path))

        if os.path.exists(destination_file):
            destination_file = create_unique_file_name(destination_file)

        if dry_run:
            logger.info("Dry run: %s %s -> %s", operation, file_path, destination_file)
            return

        os.makedirs(destination_path, exist_ok=True)

        if copy_files:
            shutil.copy2(file_path, destination_file)
        else:
            shutil.move(file_path, destination_file)
        logger.info("%s: %s -> %s", operation.capitalize(), file_path, destination_file)
    except ExifToolError as exc:
        # Unknown date is not "no date": leave the file in place rather than misfile it.
        logger.error("Skipping %s, exiftool failed: %s", file_path, exc)
    except FileNotFoundError:
        logger.warning("Skipping file that disappeared during processing: %s", file_path)
    except PermissionError as exc:
        logger.error("Permission error for %s: %s", file_path, exc)
    except OSError as exc:
        logger.error("Failed to %s %s: %s", operation, file_path, exc)


def is_same_path(path_a: str, path_b: str) -> bool:
    try:
        return os.path.samefile(path_a, path_b)
    except OSError:
        return os.path.realpath(path_a) == os.path.realpath(path_b)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Photo/video sorter using ExifTool")
    parser.add_argument("source_directory", help="Source directory")
    parser.add_argument("destination_directory", help="Destination directory")
    parser.add_argument("--dry-run", action="store_true", help="Print planned operations only")
    parser.add_argument("--copy", action="store_true", help="Copy files instead of moving them")
    parser.add_argument("--log-file", help="Optional log file path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logger(args.log_file)

    if shutil.which("exiftool") is None:
        logger.error(
            "Exiftool is not installed. Please install exiftool before running this script."
        )
        return 1

    source = args.source_directory
    destination = args.destination_directory
    if not os.path.isdir(source):
        logger.error("Source directory does not exist: %s", source)
        return 1
    if is_same_path(source, destination):
        logger.error("Source and destination must be different directories")
        return 1

    if not args.dry_run:
        os.makedirs(destination, exist_ok=True)

    try:
        session = ExifToolSession(logger)
    except OSError as exc:
        logger.error("Failed to start exiftool: %s", exc)
        return 1

    try:
        with session:
            for root, dirs, files in os.walk(source):
                # The destination may live inside the source: never re-sort what was just sorted.
                dirs[:] = [d for d in dirs if not is_same_path(os.path.join(root, d), destination)]
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    move_or_copy_file(
                        file_path,
                        destination,
                        logger,
                        dry_run=args.dry_run,
                        copy_files=args.copy,
                        session=session,
                    )
    except OSError as exc:
        logger.error("Failed while processing files: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
