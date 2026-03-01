import argparse
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from types import TracebackType

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


def extract_date_from_json(output: str, logger: logging.Logger, file_path: str) -> str | None:
    output = output.strip()
    if not output:
        logger.warning("Exiftool returned empty JSON for %s", file_path)
        return None

    try:
        exif_data = json.loads(output)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON from exiftool for %s: %s", file_path, exc)
        return None

    if not isinstance(exif_data, list) or not exif_data:
        logger.warning("Unexpected JSON structure from exiftool for %s", file_path)
        return None

    date_taken = (
        exif_data[0].get("DateTimeOriginal")
        or exif_data[0].get("CreateDate")
        or exif_data[0].get("DateCreated")
    )
    if not date_taken:
        return None

    return date_taken


class ExifToolSession:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.process = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def _write_lines(self, lines: list[str]) -> None:
        if not self.process.stdin:
            raise RuntimeError("Exiftool stdin is not available")
        for line in lines:
            self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def _read_response(self) -> str:
        if not self.process.stdout:
            raise RuntimeError("Exiftool stdout is not available")
        response_lines: list[str] = []
        while True:
            line = self.process.stdout.readline()
            if line == "":
                if self.process.poll() is not None:
                    self.logger.error("Exiftool exited unexpectedly")
                    break
                continue
            if line.strip() == READY_MARKER:
                break
            response_lines.append(line)
        return "".join(response_lines)

    def execute(self, args: list[str]) -> str:
        if self.process.poll() is not None:
            self.logger.error("Exiftool is not running")
            return ""
        try:
            self._write_lines(args + ["-execute"])
            return self._read_response()
        except OSError as exc:
            self.logger.error("Failed to communicate with exiftool: %s", exc)
            return ""

    def get_date_taken(self, file_path: str) -> str | None:
        output = self.execute(["-j", "-DateTimeOriginal", "-CreateDate", "-DateCreated", file_path])
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
            self.process.terminate()

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
            try:
                parsed_date = datetime.strptime(date_taken, "%Y:%m:%d %H:%M:%S")
                year = parsed_date.year
                month_str = str(parsed_date.month).zfill(2)
                file_extension = os.path.splitext(file_path)[1][1:].upper()
                return os.path.join(destination_directory, str(year), month_str, file_extension)
            except ValueError as exc:
                logger.warning("Invalid DateTimeOriginal for %s: %s", file_path, exc)
        return os.path.join(destination_directory, "Non_classee")
    if is_video_file(file_path):
        date_taken = session.get_date_taken(file_path)
        if date_taken is not None:
            try:
                parsed_date = datetime.strptime(date_taken, "%Y:%m:%d %H:%M:%S")
                return os.path.join(destination_directory, str(parsed_date.year), "Videos")
            except ValueError as exc:
                logger.warning("Invalid video date for %s: %s", file_path, exc)

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
    destination_path = resolve_destination(file_path, destination_directory, logger, session)
    destination_file = os.path.join(destination_path, os.path.basename(file_path))

    if os.path.exists(destination_file):
        destination_file = create_unique_file_name(destination_file)

    operation = "copy" if copy_files else "move"
    if dry_run:
        logger.info("Dry run: %s %s -> %s", operation, file_path, destination_file)
        return

    os.makedirs(destination_path, exist_ok=True)

    try:
        if copy_files:
            shutil.copy2(file_path, destination_file)
        else:
            shutil.move(file_path, destination_file)
        logger.info("%s: %s -> %s", operation.capitalize(), file_path, destination_file)
    except PermissionError as exc:
        logger.error("Permission error for %s: %s", file_path, exc)
    except OSError as exc:
        logger.error("Failed to %s %s: %s", operation, file_path, exc)


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

    if not os.path.exists(args.destination_directory):
        os.makedirs(args.destination_directory)

    try:
        with ExifToolSession(logger) as session:
            for root, _, files in os.walk(args.source_directory):
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    move_or_copy_file(
                        file_path,
                        args.destination_directory,
                        logger,
                        dry_run=args.dry_run,
                        copy_files=args.copy,
                        session=session,
                    )
    except OSError as exc:
        logger.error("Failed to start exiftool: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
