# PhotoSorter

Photo/video sorter using ExifTool.

## Requirements

- Python 3.11+
- `exiftool` installed and available on your `PATH`

## Usage

```bash
python photosorter.py /path/to/source /path/to/destination
```

### Copy instead of move

```bash
python photosorter.py /path/to/source /path/to/destination --copy
```

### Dry run (no file changes)

```bash
python photosorter.py /path/to/source /path/to/destination --dry-run
```

### Log to a file

```bash
python photosorter.py /path/to/source /path/to/destination --log-file /path/to/tri_log.txt
```

### Combine options

```bash
python photosorter.py /path/to/source /path/to/destination --dry-run --copy --log-file /path/to/tri_log.txt
```
