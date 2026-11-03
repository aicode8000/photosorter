# PhotoSorter

Photo/video sorter using ExifTool.

## Requirements

- Python 3.11+
- `exiftool` installed and available on your `PATH`

## Rangement

| Fichier | Destination |
|---|---|
| Photo datée | `AAAA/MM/EXT/` (`JPEG`→`JPG`, `HEIF`→`HEIC`, `TIF`→`TIFF`) |
| Photo sans date EXIF/XMP | `Non_classee/` |
| Vidéo | `AAAA/Videos/` (date des métadonnées, sinon date du fichier) |
| Sidecar `.xmp`, `.aae`, `.dop` | suit sa photo (même dossier, même renommage) |
| `.MOV` d'une Live Photo | à côté de sa photo |
| Doublon identique déjà présent | laissé dans la source |
| Autre fichier (`.txt`, `.pdf`, sidecar orphelin…) | laissé dans la source |

Les fichiers et dossiers système (`.DS_Store`, `Thumbs.db`, `.@__thumb`, `@eaDir`…)
sont ignorés. Un bilan est affiché en fin de traitement ; le code de sortie vaut 1
si des erreurs sont survenues.

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
