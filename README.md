<img width="1920" height="1080" alt="Screenshot (1045)" src="https://github.com/user-attachments/assets/c4959744-0f45-4910-beaa-c31aecb38dbf" />
<img width="1920" height="1080" alt="Screenshot (1043)" src="https://github.com/user-attachments/assets/d483bbed-5c40-4865-b5b2-a48358b32ffb" />

# SCD Editor — Supreme Commander Archive Tool (single file)

A self-contained Python GUI for browsing, extracting, and editing Supreme
Commander `.scd` archives — including the **Xbox 360 build**, which uses LZMA
(method 24) plus a custom data-offset index. Repacked archives load on the
console.

Everything (codec + GUI) is in one file: **`scd_editor.py`**. That makes it
easy to compile into a standalone `.exe`.

## Running

```bash
python3 scd_editor.py
python3 scd_editor.py path/to/luadata.scd   # open a file directly
```

Requires Python 3.10+ with Tkinter (bundled on Windows/macOS; on Linux
`sudo apt install python3-tk`).

Recommended optional dependency:

```
pip install pylzma
```

`pylzma` makes the encoder omit the trailing end-of-stream marker and write the
exact 14-byte LZMA header the originals use, the closest match to the game's
own tool. Without it the built-in `lzma` module is used instead; both produce
archives the console accepts and that this tool reads back.

## Building a standalone .exe (Windows)

```
pip install pyinstaller pylzma
pyinstaller --onefile --windowed scd_editor.py
```

The result is `dist/scd_editor.exe`. (`--windowed` hides the console window;
drop it if you want to see logs.)

## What it does

- **Browse** the archive as a tree, with a live name filter.
- **Preview** text files (`.lua`, `.bp`, `.txt`, ...) inline; binary files show
  a hex dump.
- **Extract** a single file, a folder, or everything.
- **Add / replace** and **delete** files (staged, applied on save).
- **Save (repack)** choosing a method:
  - **LZMA — Xbox 360** (default) — method 24 + the offset index, what the
    console needs.
  - **Deflate** / **Store** — for PC tools, not the 360.
  - **Keep each unchanged file's original method** — when saving as LZMA,
    untouched files keep their original method, so a straight repack reproduces
    the original layout. Changed/added files use the chosen method.
- **Pack a folder** into a fresh `.scd`.

Saves write to a temp file first, then atomically replace the target.

## Why the Xbox 360 build is special

The console build fatally crashes unless **both** are right:

1. **LZMA compression (method 24).** Store and Deflate crash it. The stream is
   LZMA in a small header:
   ```
   [0x00 marker][props 0x5d][dict_size u32 = uncompressed size]
   [uncompressed_size u64][raw LZMA1 data]
   ```
   (`0x5d` = lc=3, lp=0, pb=2; CRC32 is stored as 0.)

2. **The per-file data-offset index.** SupCom's loader doesn't parse ZIP local
   headers; it reads each central-directory entry's *external-attributes* field,
   which holds the absolute byte offset of that entry's compressed data, and
   seeks straight there. A normal ZIP writer leaves it 0, so the console seeks
   to offset 0 and crashes. This tool computes and writes it for every entry.

The decoder is tolerant of how a stream was produced — any dict_size value,
with or without an end-of-stream marker — so it reliably reads the originals,
this tool's output (current or older builds), and PC Store/Deflate archives.

### About the `xcompress*.dll` files

Those are Microsoft's XCompress/XMemCompress (**LZX**), a different algorithm
from the **LZMA** these archives use. They don't apply here and aren't needed.

## Compression methods

| Method | Meaning        | Use                                              |
|-------:|----------------|--------------------------------------------------|
| 0      | Store          | Uncompressed                                     |
| 8      | Deflate        | Standard ZIP; PC tools                           |
| 24     | LZMA (Xbox360) | **Required by the console game**                 |

## Using the codec from your own scripts

`scd_editor.py` doubles as an importable module:

```python
import scd_editor as scd

with scd.ScdArchive("luadata.scd") as a:
    data = a.read(a.get("lua/EffectTemplates.lua"))
    a.extract_all("out_dir")

with scd.ScdWriter("repacked.scd", method=scd.METHOD_LZMA_X360) as w:
    w.add_tree("out_dir")
    w.add_file("mods/x.lua", b"...")
    # per-file method override, e.g. keep one file stored:
    # w.add_file("big.dat", data, method=0)
```



