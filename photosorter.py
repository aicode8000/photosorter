import argparse
import filecmp
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import TextIO

RAW_EXTENSIONS = {".nef", ".dng", ".cr2", ".cr3", ".arw", ".raf", ".orf", ".rw2"}
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".webp",
} | RAW_EXTENSIONS
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mts",
    ".m2ts",
    ".avi",
    ".mkv",
    ".3gp",
    ".mpg",
    ".mpeg",
    ".wmv",
}
# Files that only make sense next to their photo: XMP metadata, iOS edits, DxO PhotoLab settings.
SIDECAR_EXTENSIONS = {".xmp", ".aae", ".dop"}
# Video half of an Apple Live Photo (IMG_0001.HEIC + IMG_0001.MOV).
LIVE_PHOTO_VIDEO_EXTENSIONS = {".mov"}
# Folder names are the extension in upper case, with aliases merged into a single folder.
EXTENSION_FOLDER_ALIASES = {"JPEG": "JPG", "HEIF": "HEIC", "TIF": "TIFF"}
# Hidden/system directories: macOS (.Trashes…), QNAP (.@__thumb, @Recycle), Synology (@eaDir…).
IGNORED_DIRECTORY_PREFIXES = (".", "@", "#")
UNCLASSIFIED_DIRECTORY = "Non_classee"
# System files that are silently ignored, like hidden files (.DS_Store, ._*).
JUNK_FILE_NAMES = {"thumbs.db", "desktop.ini", "icon\r"}

LOGGER_NAME = "photosorter"
READY_MARKER = "{ready}"
# CreationDate (QuickTime Keys, written by iPhones) carries the local time and time zone,
# so it is preferred over CreateDate for videos.
DATE_TAGS = ("DateTimeOriginal", "CreationDate", "CreateDate", "DateCreated")
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
        # QuickTime dates are UTC by spec: QuickTimeUTC converts them to local time.
        output = self.execute(
            ["-j", "-api", "QuickTimeUTC", *(f"-{tag}" for tag in DATE_TAGS), file_path]
        )
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


@dataclass
class MediaGroup:
    """A photo or video plus the files that must travel with it."""

    main: str
    companions: list[str] = field(default_factory=list)


def is_junk_file(name: str) -> bool:
    return name.startswith(".") or name.lower() in JUNK_FILE_NAMES


def is_ignored_directory(name: str) -> bool:
    return name.startswith(IGNORED_DIRECTORY_PREFIXES)


def split_name(file_name: str) -> tuple[str, str]:
    """Return (lower-case stem, lower-case extension)."""
    stem, extension = os.path.splitext(file_name)
    return stem.lower(), extension.lower()


def group_directory_files(
    directory: str, file_names: list[str]
) -> tuple[list[MediaGroup], list[str]]:
    """Group the media files of one directory with their sidecars and Live Photo videos.

    Returns the groups to sort and the paths left untouched (non-media, orphan sidecars).
    System files (.DS_Store, ._*, Thumbs.db) are neither sorted nor reported.
    """
    visible = sorted(name for name in file_names if not is_junk_file(name))
    images = [name for name in visible if split_name(name)[1] in IMAGE_EXTENSIONS]
    videos = [name for name in visible if split_name(name)[1] in VIDEO_EXTENSIONS]
    sidecars = [name for name in visible if split_name(name)[1] in SIDECAR_EXTENSIONS]

    groups = {name: MediaGroup(os.path.join(directory, name)) for name in images + videos}
    images_by_stem: dict[str, list[str]] = {}
    for name in images:
        images_by_stem.setdefault(split_name(name)[0], []).append(name)

    def pick(stem: str, candidates: list[str]) -> str | None:
        # RAW first: XMP/DxO sidecars of a RAW+JPEG pair belong to the RAW file.
        matches = [name for name in candidates if split_name(name)[0] == stem]
        matches.sort(key=lambda name: split_name(name)[1] not in RAW_EXTENSIONS)
        return matches[0] if matches else None

    for name in videos:
        stem, extension = split_name(name)
        if extension not in LIVE_PHOTO_VIDEO_EXTENSIONS:
            continue
        stills = [i for i in images_by_stem.get(stem, []) if split_name(i)[1] not in RAW_EXTENSIONS]
        if stills:
            groups[stills[0]].companions.append(groups.pop(name).main)

    untouched: list[str] = []
    for name in sidecars:
        stem = split_name(name)[0]
        # "DSC_0001.NEF.dop" names its photo exactly; "DSC_0001.xmp" only by stem.
        owner = next((main for main in groups if main.lower() == stem), None)
        owner = owner or pick(stem, list(groups))
        if owner is None:
            untouched.append(os.path.join(directory, name))
        else:
            groups[owner].companions.append(os.path.join(directory, name))

    handled = set(images + videos + sidecars)
    untouched.extend(os.path.join(directory, name) for name in visible if name not in handled)
    return list(groups.values()), untouched


def extension_folder(file_path: str) -> str:
    folder = os.path.splitext(file_path)[1][1:].upper()
    return EXTENSION_FOLDER_ALIASES.get(folder, folder)


def resolve_destination(
    file_path: str,
    destination_directory: str,
    session: ExifToolSession,
) -> str:
    if is_image_file(file_path):
        date_taken = session.get_date_taken(file_path)
        if date_taken is not None:
            return os.path.join(
                destination_directory,
                str(date_taken.year),
                f"{date_taken.month:02d}",
                extension_folder(file_path),
            )
        return os.path.join(destination_directory, UNCLASSIFIED_DIRECTORY)
    if is_video_file(file_path):
        date_taken = session.get_date_taken(file_path)
        if date_taken is not None:
            return os.path.join(destination_directory, str(date_taken.year), "Videos")

        modified_year = datetime.fromtimestamp(os.path.getmtime(file_path)).year
        return os.path.join(destination_directory, str(modified_year), "Videos")
    return os.path.join(destination_directory, UNCLASSIFIED_DIRECTORY)


def same_content(path_a: str, path_b: str) -> bool:
    return os.path.getsize(path_a) == os.path.getsize(path_b) and filecmp.cmp(
        path_a, path_b, shallow=False
    )


def find_target(
    source_file: str, target_file: str, planned: dict[str, str] | None = None
) -> tuple[str, bool]:
    """Return a free target path, or (existing path, True) if an identical copy is there.

    Identical copies are looked for under the same name and its "_N" variants, so that
    re-running the sorter on the same files does nothing. `planned` maps targets already
    claimed during this run to their source file: in dry-run nothing is written, so this
    is what keeps two same-named files from being shown the same destination.
    """
    planned = planned or {}
    name, extension = os.path.splitext(target_file)
    candidate, counter = target_file, 0
    while True:
        occupant = candidate if os.path.exists(candidate) else planned.get(candidate)
        if occupant is None:
            return candidate, False
        if same_content(source_file, occupant):
            return candidate, True
        counter += 1
        candidate = f"{name}_{counter}{extension}"


def companion_target(companion: str, main: str, main_target: str) -> str:
    """Rename a companion like its main file: DSC_0001.xmp follows DSC_0001.NEF -> DSC_0001_1."""
    old_stem = os.path.splitext(os.path.basename(main))[0]
    new_stem = os.path.splitext(os.path.basename(main_target))[0]
    companion_name = os.path.basename(companion)
    if companion_name.lower().startswith(old_stem.lower()):
        companion_name = new_stem + companion_name[len(old_stem) :]
    return os.path.join(os.path.dirname(main_target), companion_name)


def process_group(
    group: MediaGroup,
    destination_directory: str,
    logger: logging.Logger,
    dry_run: bool,
    copy_files: bool,
    session: ExifToolSession,
    planned: dict[str, str] | None = None,
) -> str:
    """Sort one group. Returns the outcome: sorted, unclassified, duplicate or error.

    `planned` (target -> source) is updated with every transfer, see find_target().
    """
    planned = {} if planned is None else planned
    file_path = group.main
    if not os.path.isfile(file_path):
        logger.warning("Skipping missing or unsupported file: %s", file_path)
        return "error"

    operation = "copy" if copy_files else "move"
    transfer = shutil.copy2 if copy_files else shutil.move
    try:
        destination_path = resolve_destination(file_path, destination_directory, session)
        main_target, duplicate = find_target(
            file_path, os.path.join(destination_path, os.path.basename(file_path)), planned
        )
        if duplicate:
            logger.info("Duplicate, left in place: %s (same as %s)", file_path, main_target)
            return "duplicate"

        transfers = [(file_path, main_target)]
        for companion in group.companions:
            target, duplicate = find_target(
                companion, companion_target(companion, file_path, main_target), planned
            )
            if duplicate:
                logger.info("Duplicate, left in place: %s (same as %s)", companion, target)
            else:
                transfers.append((companion, target))

        for source, target in transfers:
            planned[target] = source
            if dry_run:
                logger.info("Dry run: %s %s -> %s", operation, source, target)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            transfer(source, target)
            logger.info("%s: %s -> %s", operation.capitalize(), source, target)
    except ExifToolError as exc:
        # Unknown date is not "no date": leave the file in place rather than misfile it.
        logger.error("Skipping %s, exiftool failed: %s", file_path, exc)
        return "error"
    except FileNotFoundError:
        logger.warning("Skipping file that disappeared during processing: %s", file_path)
        return "error"
    except PermissionError as exc:
        logger.error("Permission error for %s: %s", file_path, exc)
        return "error"
    except OSError as exc:
        logger.error("Failed to %s %s: %s", operation, file_path, exc)
        return "error"

    if os.path.basename(destination_path) == UNCLASSIFIED_DIRECTORY:
        return "unclassified"
    return "sorted"


def remove_emptied_directories(
    directories: set[str], source_root: str, logger: logging.Logger
) -> int:
    """Remove source directories left empty by the move, then their empty parents.

    A directory counts as empty when it only holds system files (.DS_Store, ._*, Thumbs.db),
    which are deleted with it. Any other file or sub-directory keeps it. The source root
    itself is never removed. Returns the number of directories removed.
    """
    source_arg, source_root = source_root, os.path.abspath(source_root)
    removed = 0
    for directory in sorted(directories, key=lambda d: d.count(os.sep), reverse=True):
        current = os.path.abspath(directory)
        while current != source_root and current.startswith(source_root + os.sep):
            try:
                entries = [os.path.join(current, entry) for entry in os.listdir(current)]
                if not all(
                    os.path.isfile(entry) and is_junk_file(os.path.basename(entry))
                    for entry in entries
                ):
                    break
                for entry in entries:
                    os.remove(entry)
                os.rmdir(current)
            except OSError as exc:
                logger.warning("Could not remove empty directory %s: %s", current, exc)
                break
            shown = os.path.join(source_arg, os.path.relpath(current, source_root))
            logger.info("Removed empty directory: %s", shown)
            removed += 1
            current = os.path.dirname(current)
    return removed


def log_summary(logger: logging.Logger, stats: Counter[str], copy_files: bool) -> None:
    verb = "copied" if copy_files else "moved"
    logger.info(
        "Summary: %d sorted, %d %s to %s, %d duplicates left in place, "
        "%d non-media files left in place, %d empty directories removed, %d errors",
        stats["sorted"],
        stats["unclassified"],
        verb,
        UNCLASSIFIED_DIRECTORY,
        stats["duplicate"],
        stats["ignored"],
        stats["removed_directories"],
        stats["error"],
    )


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

    try:
        session = ExifToolSession(logger)
    except OSError as exc:
        logger.error("Failed to start exiftool: %s", exc)
        return 1

    stats: Counter[str] = Counter()
    planned: dict[str, str] = {}
    emptied_directories: set[str] = set()
    try:
        with session:
            for root, dirs, files in os.walk(source):
                # Skip system folders, and never re-sort a destination nested in the source.
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not is_ignored_directory(d)
                    and not is_same_path(os.path.join(root, d), destination)
                )
                groups, untouched = group_directory_files(root, files)
                for path in untouched:
                    logger.info("Not a media file, left in place: %s", path)
                stats["ignored"] += len(untouched)
                for group in groups:
                    outcome = process_group(
                        group,
                        destination,
                        logger,
                        dry_run=args.dry_run,
                        copy_files=args.copy,
                        session=session,
                        planned=planned,
                    )
                    stats[outcome] += 1
                    if outcome in ("sorted", "unclassified"):
                        emptied_directories.add(root)
    except OSError as exc:
        logger.error("Failed while processing files: %s", exc)
        return 1

    if not args.copy and not args.dry_run:
        stats["removed_directories"] = remove_emptied_directories(
            emptied_directories, source, logger
        )
    log_summary(logger, stats, args.copy)
    return 1 if stats["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
