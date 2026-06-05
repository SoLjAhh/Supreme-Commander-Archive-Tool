#!/usr/bin/env python3
"""
scd_editor.py - All-in-one GUI + codec for Supreme Commander .scd archives,
including the Xbox 360 LZMA (method 24) variant.

A .scd file is a ZIP archive. PC builds use Store/Deflate; the Xbox 360 build
uses LZMA (method 24) plus a per-entry data-offset index stored in the central
directory. This single-file tool reads all of that and writes 360-compatible
archives, and provides a GUI to browse, preview, extract, add/replace, delete,
and repack.

Self-contained (codec + GUI in one module) so it builds into a standalone .exe:
    pyinstaller --onefile --windowed scd_editor.py

Optional dependency: pylzma (closest-match LZMA encoding). Falls back to the
standard-library lzma module if absent. Requires tkinter (bundled with the
Windows/macOS Python installers).
"""

from __future__ import annotations

# ---- standard library ----
import binascii
import io
import lzma
import os
import struct
import sys
import threading
import traceback
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional: pylzma gives an LZMA encoder that can omit the end-of-stream marker
# (eos=0), producing a stream closest to the game's original tool. The stdlib
# lzma module is always used as a fallback and for decoding.
try:
    import pylzma as _pylzma
    _HAVE_PYLZMA = True
except Exception:
    _pylzma = None
    _HAVE_PYLZMA = False


# ============================================================================
# CODEC: SCD archive reader / writer
# ============================================================================

# ZIP signatures
SIG_LOCAL = b"PK\x03\x04"
SIG_CENTRAL = b"PK\x01\x02"
SIG_EOCD = b"PK\x05\x06"
SIG_EOCD64 = b"PK\x06\x06"
SIG_EOCD64_LOC = b"PK\x06\x07"

METHOD_STORE = 0
METHOD_DEFLATE = 8
METHOD_LZMA_X360 = 24  # 0x18, Supreme Commander Xbox 360 LZMA

METHOD_NAMES = {
    METHOD_STORE: "Store",
    METHOD_DEFLATE: "Deflate",
    METHOD_LZMA_X360: "LZMA (Xbox360)",
}


class ScdError(Exception):
    pass


@dataclass
class ScdEntry:
    filename: str
    method: int
    compress_size: int
    uncompress_size: int
    crc: int
    flag_bits: int
    header_offset: int          # offset of the local file header
    date_time: tuple            # (Y, M, D, h, m, s)
    is_dir: bool = False
    # filled lazily
    _data_offset: Optional[int] = field(default=None, repr=False)

    @property
    def method_name(self) -> str:
        return METHOD_NAMES.get(self.method, f"Unknown({self.method})")


def _dos_to_datetime(dosdate: int, dostime: int) -> tuple:
    day = dosdate & 0x1F
    month = (dosdate >> 5) & 0x0F
    year = ((dosdate >> 9) & 0x7F) + 1980
    sec = (dostime & 0x1F) * 2
    minute = (dostime >> 5) & 0x3F
    hour = (dostime >> 11) & 0x1F
    return (year, month, day, hour, minute, sec)


def _datetime_to_dos(dt: tuple) -> tuple:
    year, month, day, hour, minute, sec = dt
    if year < 1980:
        year = 1980
    dosdate = ((year - 1980) << 9) | (month << 5) | day
    dostime = (hour << 11) | (minute << 5) | (sec // 2)
    return dosdate, dostime


class ScdArchive:
    """Read a .scd / .zip archive, decoding all three SupCom methods."""

    def __init__(self, path: str):
        self.path = path
        self.entries: list[ScdEntry] = []
        self._fp = open(path, "rb")
        try:
            self._read_central_directory()
        except Exception:
            self._fp.close()
            raise

    # ---- context manager -------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._fp and not self._fp.closed:
            self._fp.close()

    # ---- central directory parsing --------------------------------------
    def _find_eocd(self) -> int:
        fp = self._fp
        fp.seek(0, os.SEEK_END)
        filesize = fp.tell()
        # EOCD is at most 22 + 65535 bytes from the end
        max_back = min(filesize, 22 + 65536)
        fp.seek(filesize - max_back)
        tail = fp.read(max_back)
        idx = tail.rfind(SIG_EOCD)
        if idx < 0:
            raise ScdError("Not a ZIP/SCD archive: end-of-central-directory not found.")
        return filesize - max_back + idx

    def _read_central_directory(self):
        fp = self._fp
        eocd_off = self._find_eocd()
        fp.seek(eocd_off)
        eocd = fp.read(22)
        (_, disk, cd_disk, n_disk, n_total,
         cd_size, cd_offset, comment_len) = struct.unpack("<IHHHHIIH", eocd)

        # Handle ZIP64 if the 32-bit fields are saturated.
        if cd_offset == 0xFFFFFFFF or n_total == 0xFFFF:
            cd_offset, n_total = self._read_zip64(eocd_off)

        fp.seek(cd_offset)
        for _ in range(n_total):
            sig = fp.read(4)
            if sig != SIG_CENTRAL:
                break
            hdr = fp.read(42)
            (ver_made, ver_need, flag, method, mtime, mdate, crc,
             csize, usize, nlen, elen, clen, disk_start,
             int_attr, ext_attr, lho) = struct.unpack("<HHHHHHIIIHHHHHII", hdr)
            name = fp.read(nlen)
            extra = fp.read(elen)
            fp.read(clen)  # comment

            # ZIP64 extra-field fixups for big entries
            if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF or lho == 0xFFFFFFFF:
                usize, csize, lho = self._patch_zip64_extra(
                    extra, usize, csize, lho)

            filename = name.decode("utf-8", "replace").replace("\\", "/")
            is_dir = filename.endswith("/")
            self.entries.append(ScdEntry(
                filename=filename,
                method=method,
                compress_size=csize,
                uncompress_size=usize,
                crc=crc,
                flag_bits=flag,
                header_offset=lho,
                date_time=_dos_to_datetime(mdate, mtime),
                is_dir=is_dir,
            ))

    def _read_zip64(self, eocd_off: int):
        fp = self._fp
        # locator sits 20 bytes before EOCD
        fp.seek(eocd_off - 20)
        loc = fp.read(20)
        if loc[:4] != SIG_EOCD64_LOC:
            raise ScdError("ZIP64 locator missing.")
        _, _, eocd64_off, _ = struct.unpack("<IIQI", loc)
        fp.seek(eocd64_off)
        rec = fp.read(56)
        if rec[:4] != SIG_EOCD64:
            raise ScdError("ZIP64 EOCD missing.")
        (_, size_rec, ver_made, ver_need, disk, cd_disk,
         n_disk, n_total, cd_size, cd_offset) = struct.unpack(
            "<IQHHIIQQQQ", rec)
        return cd_offset, n_total

    @staticmethod
    def _patch_zip64_extra(extra: bytes, usize, csize, lho):
        i = 0
        while i + 4 <= len(extra):
            hid, hsz = struct.unpack("<HH", extra[i:i + 4])
            body = extra[i + 4:i + 4 + hsz]
            if hid == 0x0001:  # ZIP64 extended info
                off = 0
                if usize == 0xFFFFFFFF:
                    usize = struct.unpack("<Q", body[off:off + 8])[0]; off += 8
                if csize == 0xFFFFFFFF:
                    csize = struct.unpack("<Q", body[off:off + 8])[0]; off += 8
                if lho == 0xFFFFFFFF:
                    lho = struct.unpack("<Q", body[off:off + 8])[0]; off += 8
                break
            i += 4 + hsz
        return usize, csize, lho

    # ---- per-entry data offset ------------------------------------------
    def _data_offset(self, entry: ScdEntry) -> int:
        if entry._data_offset is not None:
            return entry._data_offset
        fp = self._fp
        fp.seek(entry.header_offset)
        sig = fp.read(4)
        if sig != SIG_LOCAL:
            raise ScdError(f"Bad local header for {entry.filename!r}.")
        lh = fp.read(26)
        (ver, flag, method, mt, md, crc, csize, usize, nlen, elen) = struct.unpack(
            "<HHHHHIIIHH", lh)
        entry._data_offset = entry.header_offset + 30 + nlen + elen
        return entry._data_offset

    # ---- decompression ---------------------------------------------------
    def read(self, entry: ScdEntry) -> bytes:
        if entry.is_dir:
            return b""
        off = self._data_offset(entry)
        self._fp.seek(off)
        raw = self._fp.read(entry.compress_size)

        if entry.method == METHOD_STORE:
            data = raw
        elif entry.method == METHOD_DEFLATE:
            data = zlib.decompress(raw, -15)
        elif entry.method == METHOD_LZMA_X360:
            data = self._decode_lzma_x360(raw, entry.uncompress_size)
        else:
            raise ScdError(
                f"Unsupported compression method {entry.method} "
                f"for {entry.filename!r}.")

        if len(data) != entry.uncompress_size:
            raise ScdError(
                f"Size mismatch for {entry.filename!r}: got {len(data)}, "
                f"expected {entry.uncompress_size}.")
        return data

    @staticmethod
    def _decode_lzma_x360(raw: bytes, expected_size: int) -> bytes:
        """Decode a Supreme Commander Xbox 360 method-24 LZMA stream.

        The stream is: [0x00 marker][props byte][dict_size u32][usize u64][LZMA1].

        This decoder is deliberately tolerant of how the stream was produced,
        so it reads the game's originals and anything this tool (current or
        older) ever wrote:

          * The dict_size field stored in the header may be the uncompressed
            size, a power-of-two, or anything else. We ignore it for sizing and
            use a dictionary at least as large as the output, which is always
            sufficient (LZMA never references past the start of the data).
          * The raw stream may or may not carry an end-of-stream marker. We
            decode with the known output size and stop there, so a trailing
            marker (or trailing padding) is harmless.
          * We parse lc/lp/pb from the props byte rather than assuming 0x5d.

        Decoding the raw LZMA1 payload directly (FORMAT_RAW) avoids the
        brittleness of FORMAT_ALONE, which trusts the header's dict_size and
        rejects size mismatches.
        """
        if not raw or expected_size == 0:
            return b""
        if len(raw) < 14:
            raise ScdError("LZMA stream too short.")

        props = raw[1]
        if props >= 9 * 5 * 5:
            raise ScdError(f"Invalid LZMA props byte 0x{props:02x}.")
        lc = props % 9
        lp = (props // 9) % 5
        pb = props // 45
        payload = raw[14:]  # skip [marker][props][dict u32][usize u64]

        # Dictionary must cover the whole output; round up to a sane minimum.
        dict_size = max(1 << 12, 1 << max(0, (expected_size - 1).bit_length()))
        dict_size = min(dict_size, 1 << 30)

        filt = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                 "lc": lc, "lp": lp, "pb": pb}]
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filt)
        try:
            out = dec.decompress(payload, expected_size)
        except lzma.LZMAError as e:
            raise ScdError(f"LZMA decode failed: {e}")
        if len(out) < expected_size:
            # try draining without the size cap in case of an early stop
            try:
                out += dec.decompress(b"", expected_size - len(out))
            except lzma.LZMAError:
                pass
        if len(out) != expected_size:
            raise ScdError(
                f"LZMA produced {len(out)} bytes, expected {expected_size}.")
        return out[:expected_size]

    # ---- convenience -----------------------------------------------------
    def get(self, filename: str) -> ScdEntry:
        for e in self.entries:
            if e.filename == filename:
                return e
        raise KeyError(filename)

    def extract_all(self, dest_dir: str,
                    progress: Optional[Callable[[int, int, str], None]] = None):
        total = len(self.entries)
        for i, entry in enumerate(self.entries, 1):
            target = os.path.join(dest_dir, entry.filename)
            if entry.is_dir:
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                data = self.read(entry)
                with open(target, "wb") as f:
                    f.write(data)
            if progress:
                progress(i, total, entry.filename)


# --------------------------------------------------------------------------
# Encoder for the Xbox 360 method-24 LZMA stream.
# --------------------------------------------------------------------------
# Props byte 0x5d == lc=3, lp=0, pb=2 (the value the original archives use).
LZMA_PROPS_BYTE = 0x5D
_MAX_DICT_LOG = 26  # cap dictionary at 64 MB


def _dict_log_for(usize: int) -> int:
    if usize <= 1:
        return 12
    log = (usize - 1).bit_length()
    return max(12, min(_MAX_DICT_LOG, log))


def encode_lzma_x360(data: bytes) -> bytes:
    """Encode `data` into a Supreme Commander Xbox 360 method-24 stream.

    Layout produced (matching the original archives):
        [0x00 marker][props=0x5d][dict_size:uint32 LE][usize:uint64 LE][LZMA1 data]

    The dict_size field is written as the uncompressed size, exactly as the
    original tool does (the encoder still uses a real power-of-two dictionary
    internally, which is large enough to cover the data). pylzma (eos=0) is
    used when available to omit the end-of-stream marker; otherwise the stdlib
    `lzma` raw encoder is used (its end marker is never reached, since the
    loader stops at `usize` bytes and seeks each entry by its stored offset).
    """
    usize = len(data)
    if usize == 0:
        return b""
    dlog = _dict_log_for(usize)
    enc_dict = 1 << dlog  # actual dictionary used for compression

    if _HAVE_PYLZMA:
        # pylzma prepends its own 5-byte props header; strip it and write ours.
        out = _pylzma.compress(
            data, dictionary=dlog, fastBytes=273,
            literalContextBits=3, literalPosBits=0, posBits=2, eos=0)
        body = out[5:]
    else:
        filt = [{"id": lzma.FILTER_LZMA1, "dict_size": enc_dict,
                 "lc": 3, "lp": 0, "pb": 2}]
        comp = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=filt)
        body = comp.compress(data) + comp.flush()

    # The header's dict_size field mirrors the uncompressed size, as the
    # original SupCom encoder wrote it.
    dict_size = usize
    header = (bytes([0x00, LZMA_PROPS_BYTE])
              + struct.pack("<I", dict_size)
              + struct.pack("<Q", usize))
    return header + body


# --------------------------------------------------------------------------
# Writer: re-pack a directory (or a set of entries) into a ZIP/SCD archive.
# --------------------------------------------------------------------------
class ScdWriter:
    """Write a ZIP-based .scd archive using a chosen compression method.

    `method` may be:
        METHOD_STORE     (0)  - uncompressed; fast in-game load on PC
        METHOD_DEFLATE   (8)  - standard ZIP compression
        METHOD_LZMA_X360 (24) - the Xbox 360 LZMA method (required by the
                                console game, which crashes on Store/Deflate)

    For backward compatibility, the boolean `compress` argument still works:
    compress=True -> Deflate, compress=False -> Store. If `method` is given it
    takes precedence.
    """

    def __init__(self, path: str, compress: bool = True, level: int = 6,
                 method: Optional[int] = None):
        if method is None:
            method = METHOD_DEFLATE if compress else METHOD_STORE
        if method not in (METHOD_STORE, METHOD_DEFLATE, METHOD_LZMA_X360):
            raise ScdError(f"Unsupported write method {method}.")
        self.path = path
        self.method = method
        self.level = level
        self._fp = open(path, "wb")
        self._central = []  # list of dicts for central directory
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.finish()

    def _write(self, data: bytes):
        self._fp.write(data)
        self._offset += len(data)

    def _compress_body(self, data: bytes, method: Optional[int] = None):
        """Return (method_used, body_bytes) for this entry.

        `method` overrides the archive default for this single file.
        """
        m = self.method if method is None else method
        usize = len(data)
        if usize == 0:
            # empty entries are always stored
            return METHOD_STORE, b""
        if m == METHOD_DEFLATE:
            comp = zlib.compressobj(self.level, zlib.DEFLATED, -15)
            return METHOD_DEFLATE, comp.compress(data) + comp.flush()
        if m == METHOD_LZMA_X360:
            return METHOD_LZMA_X360, encode_lzma_x360(data)
        return METHOD_STORE, data

    def add_file(self, arcname: str, data: bytes, date_time: tuple = None,
                 method: Optional[int] = None):
        arcname = arcname.replace("\\", "/")
        if date_time is None:
            import time
            date_time = time.localtime()[:6]
        # Xbox 360 archives store CRC32 = 0 for LZMA entries; match that so the
        # repacked archive looks like the originals. Store/Deflate get real CRCs.
        usize = len(data)
        used_method, body = self._compress_body(data, method)
        crc = 0 if used_method == METHOD_LZMA_X360 else (binascii.crc32(data) & 0xFFFFFFFF)
        csize = len(body)
        method = used_method

        dosdate, dostime = _datetime_to_dos(date_time)
        name_b = arcname.encode("utf-8")
        local_off = self._offset
        # The original SupCom archives use version-needed 10 on every entry.
        ver_need = 10

        # local file header
        self._write(SIG_LOCAL)
        self._write(struct.pack(
            "<HHHHHIIIHH",
            ver_need, 0, method, dostime, dosdate, crc, csize, usize,
            len(name_b), 0))
        self._write(name_b)
        # SupCom indexes each entry by the absolute offset of its compressed
        # data, stored in the central-directory "external attributes" field.
        # The Xbox 360 loader seeks there directly; if it is wrong/zero the
        # game reads garbage and crashes. Record it now (after the local
        # header + name, before the body).
        data_offset = self._offset
        self._write(body)

        self._central.append(dict(
            method=method, dostime=dostime, dosdate=dosdate, crc=crc,
            csize=csize, usize=usize, name=name_b, offset=local_off,
            data_offset=data_offset, ver_need=ver_need,
            is_dir=arcname.endswith("/")))

    def add_dir(self, arcname: str, date_time: tuple = None):
        if not arcname.endswith("/"):
            arcname += "/"
        self.add_file(arcname, b"", date_time)

    def add_tree(self, root_dir: str,
                 progress: Optional[Callable[[int, int, str], None]] = None):
        files = []
        for base, dirs, names in os.walk(root_dir):
            for n in names:
                files.append(os.path.join(base, n))
        total = len(files)
        for i, full in enumerate(files, 1):
            arc = os.path.relpath(full, root_dir).replace(os.sep, "/")
            with open(full, "rb") as f:
                data = f.read()
            import time
            mtime = time.localtime(os.path.getmtime(full))[:6]
            self.add_file(arc, data, mtime)
            if progress:
                progress(i, total, arc)

    def finish(self):
        if self._fp.closed:
            return
        cd_start = self._offset
        for c in self._central:
            self._write(SIG_CENTRAL)
            # External-attributes field carries the absolute offset of this
            # entry's compressed data — SupCom's direct-seek index.
            ext_attr = c.get("data_offset", 0)
            self._write(struct.pack(
                "<HHHHHHIIIHHHHHII",
                20, c.get("ver_need", 20), 0, c["method"], c["dostime"],
                c["dosdate"], c["crc"], c["csize"], c["usize"], len(c["name"]),
                0, 0, 0, 0, ext_attr, c["offset"]))
            self._write(c["name"])
        cd_size = self._offset - cd_start
        n = len(self._central)
        self._write(SIG_EOCD)
        # fields after signature: disk, cd_disk, n_disk, n_total,
        # cd_size, cd_offset, comment_len
        self._write(struct.pack(
            "<HHHHIIH", 0, 0, n, n, cd_size, cd_start, 0))
        self._fp.close()


# ============================================================================
# GUI: Tkinter editor
# ============================================================================


APP_TITLE = "SCD Editor — Supreme Commander Archive Tool"

TEXT_EXTS = {
    ".lua", ".txt", ".bp", ".md", ".cfg", ".ini", ".log", ".json",
    ".xml", ".csv", ".nfo", ".fdf",
}
PREVIEW_LIMIT = 512 * 1024  # bytes


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1024**(['B','KB','MB','GB'].index(unit)):.1f} {unit}"
        n_div = n
    return f"{n} B"


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"


class PendingChange:
    """A staged add/replace; data lives in memory until repack."""
    __slots__ = ("data",)
    def __init__(self, data: bytes):
        self.data = data


class ScdEditorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x680")
        self.minsize(820, 520)

        self.archive: ScdArchive | None = None
        self.archive_path: str | None = None
        # staged changes for repack
        self.pending: dict[str, PendingChange] = {}   # arcname -> new data
        self.deleted: set[str] = set()                # arcnames removed
        # map tree item id -> arcname (files only)
        self.item_to_name: dict[str, str] = {}
        self.dirty = False

        self._build_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_status("Open a .scd archive to begin.")
        self._refresh_actions()

    # ------------------------------------------------------------------ UI
    def _build_style(self):
        self.configure(bg="#1f242b")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg = "#1f242b"; panel = "#272d36"; fg = "#dfe5ec"; acc = "#4ea1d3"
        sub = "#8b95a3"
        style.configure(".", background=bg, foreground=fg,
                        fieldbackground=panel, font=("Segoe UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Sub.TLabel", background=bg, foreground=sub,
                        font=("Segoe UI", 9))
        style.configure("Head.TLabel", background=bg, foreground=acc,
                        font=("Segoe UI Semibold", 11))
        style.configure("TButton", background=panel, foreground=fg,
                        borderwidth=0, padding=(12, 6), focuscolor=panel)
        style.map("TButton",
                  background=[("active", "#33404e"), ("disabled", "#23282f")],
                  foreground=[("disabled", "#5a626d")])
        style.configure("Accent.TButton", background=acc, foreground="#10161c",
                        font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton",
                  background=[("active", "#62b4e6"), ("disabled", "#2f3a44")])
        style.configure("Treeview", background=panel, fieldbackground=panel,
                        foreground=fg, borderwidth=0, rowheight=24)
        style.configure("Treeview.Heading", background="#2f3742",
                        foreground=sub, borderwidth=0,
                        font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#37506b")],
                  foreground=[("selected", "#ffffff")])
        style.configure("TProgressbar", background=acc, troughcolor=panel,
                        borderwidth=0)
        self._c = dict(bg=bg, panel=panel, fg=fg, acc=acc, sub=sub)

    def _build_menu(self):
        m = tk.Menu(self)
        filem = tk.Menu(m, tearoff=0)
        filem.add_command(label="Open .scd…", command=self.open_archive,
                          accelerator="Ctrl+O")
        filem.add_separator()
        filem.add_command(label="Save (repack)…", command=self.save_archive,
                          accelerator="Ctrl+S")
        filem.add_command(label="Extract All…", command=self.extract_all)
        filem.add_separator()
        filem.add_command(label="Pack folder into new .scd…",
                          command=self.pack_folder)
        filem.add_separator()
        filem.add_command(label="Exit", command=self._on_close)
        m.add_cascade(label="File", menu=filem)

        helpm = tk.Menu(m, tearoff=0)
        helpm.add_command(label="About", command=self._about)
        m.add_cascade(label="Help", menu=helpm)
        self.config(menu=m)

        self.bind_all("<Control-o>", lambda e: self.open_archive())
        self.bind_all("<Control-s>", lambda e: self.save_archive())

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(12, 10))
        bar.pack(fill="x")
        self.btn_open = ttk.Button(bar, text="Open .scd", style="Accent.TButton",
                                   command=self.open_archive)
        self.btn_open.pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)

        self.btn_extract_sel = ttk.Button(bar, text="Extract selected",
                                           command=self.extract_selected)
        self.btn_extract_sel.pack(side="left", padx=(0, 6))
        self.btn_extract_all = ttk.Button(bar, text="Extract all",
                                           command=self.extract_all)
        self.btn_extract_all.pack(side="left", padx=(0, 6))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        self.btn_add = ttk.Button(bar, text="Add / replace…",
                                   command=self.add_files)
        self.btn_add.pack(side="left", padx=(0, 6))
        self.btn_del = ttk.Button(bar, text="Delete", command=self.delete_selected)
        self.btn_del.pack(side="left", padx=(0, 6))

        self.btn_save = ttk.Button(bar, text="Save (repack)",
                                   style="Accent.TButton", command=self.save_archive)
        self.btn_save.pack(side="right")

    def _build_body(self):
        body = ttk.Frame(self, padding=(12, 0, 12, 6))
        body.pack(fill="both", expand=True)

        paned = ttk.PanedWindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # ---- left: filter + tree
        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        fr = ttk.Frame(left)
        fr.pack(fill="x", pady=(0, 6))
        ttk.Label(fr, text="Filter:", style="Sub.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self._apply_filter())
        ent = tk.Entry(fr, textvariable=self.filter_var, bg=self._c["panel"],
                       fg=self._c["fg"], insertbackground=self._c["fg"],
                       relief="flat", highlightthickness=1,
                       highlightbackground="#36404b",
                       highlightcolor=self._c["acc"])
        ent.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)

        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=("size", "method"),
                                 selectmode="extended")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("method", text="Method")
        self.tree.column("#0", width=380, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("method", width=120, anchor="w")
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.tag_configure("added", foreground="#7fdca0")
        self.tree.tag_configure("deleted", foreground="#e0747c")

        # ---- right: preview
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.preview_title = ttk.Label(right, text="Preview", style="Head.TLabel")
        self.preview_title.pack(anchor="w", pady=(0, 4))
        self.preview_info = ttk.Label(right, text="", style="Sub.TLabel")
        self.preview_info.pack(anchor="w", pady=(0, 6))
        pv = ttk.Frame(right)
        pv.pack(fill="both", expand=True)
        self.preview = tk.Text(pv, wrap="none", bg="#171b21", fg="#cdd6e0",
                               insertbackground="#cdd6e0", relief="flat",
                               font=("Cascadia Mono", 10), padx=10, pady=8)
        pvb = ttk.Scrollbar(pv, orient="vertical", command=self.preview.yview)
        pvh = ttk.Scrollbar(pv, orient="horizontal", command=self.preview.xview)
        self.preview.configure(yscrollcommand=pvb.set, xscrollcommand=pvh.set)
        self.preview.grid(row=0, column=0, sticky="nsew")
        pvb.grid(row=0, column=1, sticky="ns")
        pvh.grid(row=1, column=0, sticky="ew")
        pv.rowconfigure(0, weight=1); pv.columnconfigure(0, weight=1)
        self.preview.configure(state="disabled")

    def _build_statusbar(self):
        sb = ttk.Frame(self, padding=(12, 6))
        sb.pack(fill="x")
        self.status = ttk.Label(sb, text="", style="Sub.TLabel")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(sb, length=220, mode="determinate")
        self.progress.pack(side="right")
        self.progress.pack_forget()

    # -------------------------------------------------------------- helpers
    def _set_status(self, text):
        self.status.config(text=text)
        self.update_idletasks()

    def _refresh_actions(self):
        has = self.archive is not None
        state = "normal" if has else "disabled"
        for b in (self.btn_extract_sel, self.btn_extract_all, self.btn_add,
                  self.btn_del, self.btn_save):
            b.config(state=state)

    def _mark_dirty(self, val=True):
        self.dirty = val
        title = APP_TITLE
        if self.archive_path:
            title = f"{os.path.basename(self.archive_path)}{' *' if val else ''} — {APP_TITLE}"
        self.title(title)

    # ----------------------------------------------------------- open / load
    def open_archive(self):
        path = filedialog.askopenfilename(
            title="Open .scd archive",
            filetypes=[("SCD / ZIP archives", "*.scd *.zip"), ("All files", "*.*")])
        if not path:
            return
        self._load(path)

    def _load(self, path):
        try:
            if self.archive:
                self.archive.close()
            self.archive = ScdArchive(path)
            self.archive_path = path
            self.pending.clear()
            self.deleted.clear()
            self._mark_dirty(False)
            self._populate_tree()
            n = len([e for e in self.archive.entries if not e.is_dir])
            self._set_status(f"Loaded {n} files from {os.path.basename(path)}")
            self._refresh_actions()
        except Exception as e:
            messagebox.showerror("Open failed", f"{e}\n\n{traceback.format_exc()}")

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.item_to_name.clear()
        self._dir_nodes = {"": ""}  # path -> tree id

        entries = sorted(self.archive.entries, key=lambda e: e.filename.lower())
        filt = self.filter_var.get().strip().lower()
        for e in entries:
            if e.is_dir:
                continue
            if e.filename in self.deleted:
                continue
            if filt and filt not in e.filename.lower():
                continue
            self._insert_path(e)

        # staged additions that are brand-new names
        for arc in sorted(self.pending):
            if any(en.filename == arc for en in self.archive.entries):
                continue
            if filt and filt not in arc.lower():
                continue
            self._insert_added(arc)

    def _ensure_dir(self, dirpath):
        if dirpath in self._dir_nodes:
            return self._dir_nodes[dirpath]
        parent = dirpath.rsplit("/", 1)[0] if "/" in dirpath else ""
        parent_id = self._ensure_dir(parent) if parent else ""
        name = dirpath.rsplit("/", 1)[-1]
        nid = self.tree.insert(parent_id, "end", text="  " + name,
                               values=("", ""), open=False)
        self._dir_nodes[dirpath] = nid
        return nid

    def _insert_path(self, e: ScdEntry):
        dirpath = e.filename.rsplit("/", 1)[0] if "/" in e.filename else ""
        parent = self._ensure_dir(dirpath) if dirpath else ""
        name = e.filename.rsplit("/", 1)[-1]
        staged = e.filename in self.pending
        size = self.pending[e.filename].data.__len__() if staged else e.uncompress_size
        method = "Replaced" if staged else e.method_name
        tags = ("added",) if staged else ()
        nid = self.tree.insert(parent, "end", text="  " + name,
                               values=(fmt_size(size), method), tags=tags)
        self.item_to_name[nid] = e.filename

    def _insert_added(self, arc: str):
        dirpath = arc.rsplit("/", 1)[0] if "/" in arc else ""
        parent = self._ensure_dir(dirpath) if dirpath else ""
        name = arc.rsplit("/", 1)[-1]
        nid = self.tree.insert(parent, "end", text="  " + name,
                               values=(fmt_size(len(self.pending[arc].data)), "Added (new)"),
                               tags=("added",))
        self.item_to_name[nid] = arc

    def _apply_filter(self):
        if self.archive:
            self._populate_tree()

    # ----------------------------------------------------------- selection
    def _selected_names(self) -> list[str]:
        """All file arcnames under the current selection (expands folders)."""
        names = set()
        for item in self.tree.selection():
            self._collect(item, names)
        return sorted(names)

    def _collect(self, item, names: set):
        if item in self.item_to_name:
            names.add(self.item_to_name[item])
        for child in self.tree.get_children(item):
            self._collect(child, names)

    def _read_current(self, arcname: str) -> bytes:
        if arcname in self.pending:
            return self.pending[arcname].data
        entry = self.archive.get(arcname)
        return self.archive.read(entry)

    def _on_select(self):
        sel = self.tree.selection()
        if len(sel) != 1 or sel[0] not in self.item_to_name:
            self._clear_preview()
            return
        arc = self.item_to_name[sel[0]]
        self._preview(arc)

    def _clear_preview(self):
        self.preview_title.config(text="Preview")
        self.preview_info.config(text="")
        self.preview.config(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.config(state="disabled")

    def _preview(self, arc: str):
        self.preview_title.config(text=os.path.basename(arc))
        ext = os.path.splitext(arc)[1].lower()
        try:
            data = self._read_current(arc)
        except Exception as e:
            self.preview_info.config(text=f"Error: {e}")
            return
        meta = f"{arc}    ·    {fmt_size(len(data))}"
        if arc in self.pending:
            meta += "    ·    (staged)"
        self.preview_info.config(text=meta)

        self.preview.config(state="normal")
        self.preview.delete("1.0", "end")
        if ext in TEXT_EXTS or self._looks_text(data[:4096]):
            chunk = data[:PREVIEW_LIMIT]
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                text = chunk.decode("latin-1", "replace")
            self.preview.insert("1.0", text)
            if len(data) > PREVIEW_LIMIT:
                self.preview.insert("end",
                    f"\n\n… preview truncated at {fmt_size(PREVIEW_LIMIT)} "
                    f"of {fmt_size(len(data))}.")
        else:
            self.preview.insert("1.0", self._hexdump(data[:2048]))
            self.preview.insert("end",
                f"\n\n[binary file — {fmt_size(len(data))}; showing first 2 KB as hex]")
        self.preview.config(state="disabled")

    @staticmethod
    def _looks_text(sample: bytes) -> bool:
        if not sample:
            return True
        if b"\x00" in sample:
            return False
        printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
        return printable / len(sample) > 0.85

    @staticmethod
    def _hexdump(data: bytes) -> str:
        lines = []
        for off in range(0, len(data), 16):
            chunk = data[off:off + 16]
            hexpart = " ".join(f"{b:02x}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
            lines.append(f"{off:08x}  {hexpart:<47}  {asc}")
        return "\n".join(lines)

    # ----------------------------------------------------------- extract
    def extract_selected(self):
        names = self._selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select a file or folder first.")
            return
        if len(names) == 1:
            base = os.path.basename(names[0])
            dest = filedialog.asksaveasfilename(
                title="Extract file as", initialfile=base)
            if not dest:
                return
            try:
                with open(dest, "wb") as f:
                    f.write(self._read_current(names[0]))
                self._set_status(f"Extracted {base}")
            except Exception as e:
                messagebox.showerror("Extract failed", str(e))
            return
        dest_dir = filedialog.askdirectory(title="Extract selected into folder")
        if not dest_dir:
            return
        self._run_bg("Extracting…", self._do_extract, names, dest_dir)

    def _do_extract(self, names, dest_dir, report):
        total = len(names)
        for i, arc in enumerate(names, 1):
            target = os.path.join(dest_dir, arc)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "wb") as f:
                f.write(self._read_current(arc))
            report(i, total, arc)
        return f"Extracted {total} files into {dest_dir}"

    def extract_all(self):
        if not self.archive:
            return
        dest_dir = filedialog.askdirectory(title="Extract all into folder")
        if not dest_dir:
            return
        names = [e.filename for e in self.archive.entries
                 if not e.is_dir and e.filename not in self.deleted]
        names += [a for a in self.pending
                  if not any(e.filename == a for e in self.archive.entries)]
        self._run_bg("Extracting all…", self._do_extract, sorted(set(names)), dest_dir)

    # ----------------------------------------------------------- edit
    def add_files(self):
        if not self.archive:
            return
        paths = filedialog.askopenfilenames(title="Add or replace files")
        if not paths:
            return
        # ask for a base path inside the archive
        base = self._ask_arc_prefix()
        if base is None:
            return
        added = 0
        for p in paths:
            arc = (base + os.path.basename(p)).replace("\\", "/").lstrip("/")
            with open(p, "rb") as f:
                self.pending[arc] = PendingChange(f.read())
            self.deleted.discard(arc)
            added += 1
        self._mark_dirty(True)
        self._populate_tree()
        self._set_status(f"Staged {added} file(s) for add/replace. Save to apply.")

    def _ask_arc_prefix(self):
        # default to the folder of the current selection, if any
        default = ""
        sel = self.tree.selection()
        if sel:
            arc = self.item_to_name.get(sel[0])
            if arc and "/" in arc:
                default = arc.rsplit("/", 1)[0] + "/"
        dlg = _PrefixDialog(self, default)
        self.wait_window(dlg)
        return dlg.result

    def delete_selected(self):
        names = self._selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select a file or folder first.")
            return
        if not messagebox.askyesno(
                "Delete", f"Stage {len(names)} file(s) for deletion?\n"
                          "This only affects the next saved archive."):
            return
        for arc in names:
            if arc in self.pending and not any(
                    e.filename == arc for e in self.archive.entries):
                # was a brand-new staged add → just drop it
                del self.pending[arc]
            else:
                self.deleted.add(arc)
                self.pending.pop(arc, None)
        self._mark_dirty(True)
        self._populate_tree()
        self._clear_preview()
        self._set_status(f"Staged {len(names)} file(s) for deletion. Save to apply.")

    # ----------------------------------------------------------- save / pack
    def save_archive(self):
        if not self.archive:
            return
        dest = filedialog.asksaveasfilename(
            title="Save repacked .scd", defaultextension=".scd",
            initialfile=os.path.basename(self.archive_path or "out.scd"),
            filetypes=[("SCD archive", "*.scd"), ("ZIP archive", "*.zip")])
        if not dest:
            return
        dlg = _MethodDialog(self, allow_preserve=True)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        method, preserve = dlg.result
        self._run_bg("Repacking…", self._do_save, dest, method, preserve)

    def _do_save(self, dest, method, preserve, report):
        # Build final name list: existing (minus deleted) + staged
        entries = [e for e in self.archive.entries
                   if not e.is_dir and e.filename not in self.deleted]
        existing_names = {e.filename for e in entries}
        new_names = [a for a in self.pending if a not in existing_names]
        total = len(entries) + len(new_names)

        # write to a temp file first, then move into place
        tmp = dest + ".tmp"
        i = 0
        with ScdWriter(tmp, method=method) as w:
            for e in entries:
                if e.filename in self.pending:
                    data = self.pending[e.filename].data
                    per = None  # changed files use the chosen method
                else:
                    data = self.archive.read(e)
                    # preserve mode keeps each untouched file's original method
                    per = e.method if preserve else None
                w.add_file(e.filename, data, e.date_time, method=per)
                i += 1
                report(i, total, e.filename)
            for arc in new_names:
                w.add_file(arc, self.pending[arc].data)
                i += 1
                report(i, total, arc)
        os.replace(tmp, dest)
        return ("__reload__", dest, f"Saved {total} files to {os.path.basename(dest)}")

    def pack_folder(self):
        folder = filedialog.askdirectory(title="Choose folder to pack")
        if not folder:
            return
        dest = filedialog.asksaveasfilename(
            title="Save new .scd", defaultextension=".scd",
            filetypes=[("SCD archive", "*.scd"), ("ZIP archive", "*.zip")])
        if not dest:
            return
        dlg = _MethodDialog(self, allow_preserve=False)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        method, _ = dlg.result
        self._run_bg("Packing folder…", self._do_pack, folder, dest, method)

    def _do_pack(self, folder, dest, method, report):
        with ScdWriter(dest, method=method) as w:
            w.add_tree(folder, progress=report)
        return f"Packed folder into {os.path.basename(dest)}"

    # ----------------------------------------------------------- threading
    def _run_bg(self, label, func, *args):
        self.progress.pack(side="right")
        self.progress.config(value=0, maximum=100)
        self._set_status(label)
        for child in (self.btn_save, self.btn_open):
            child.config(state="disabled")

        def report(i, total, name=""):
            pct = (i / total * 100) if total else 100
            self.after(0, lambda: (self.progress.config(value=pct),
                                   self._set_status(f"{label}  {i}/{total}  {name[-48:]}")))

        def worker():
            try:
                result = func(*args, report)
                self.after(0, lambda: self._bg_done(result))
            except Exception as e:
                tb = traceback.format_exc()
                self.after(0, lambda: self._bg_error(e, tb))

        threading.Thread(target=worker, daemon=True).start()

    def _bg_done(self, result):
        self.progress.pack_forget()
        self.btn_open.config(state="normal")
        self._refresh_actions()
        # reload-after-save signal
        if isinstance(result, tuple) and result and result[0] == "__reload__":
            _, path, msg = result
            self._set_status(msg)
            if messagebox.askyesno("Saved", msg + "\n\nReload the saved archive now?"):
                self._load(path)
            else:
                self._mark_dirty(False)
            return
        self._set_status(result or "Done.")

    def _bg_error(self, e, tb):
        self.progress.pack_forget()
        self.btn_open.config(state="normal")
        self._refresh_actions()
        messagebox.showerror("Operation failed", f"{e}\n\n{tb}")
        self._set_status("Error.")

    # ----------------------------------------------------------- misc
    def _about(self):
        messagebox.showinfo(
            "About",
            "SCD Editor\n\n"
            "Reads and edits Supreme Commander .scd archives, including the "
            "Xbox 360 LZMA (method 24) variant that standard ZIP tools "
            "cannot open.\n\n"
            "Repacks to standard ZIP (Store or Deflate), which the game and "
            "all archive tools accept.\n\n"
            "Pure Python standard library.")

    def _on_close(self):
        if self.dirty and not messagebox.askyesno(
                "Unsaved changes",
                "You have staged changes that haven't been saved.\nQuit anyway?"):
            return
        if self.archive:
            self.archive.close()
        self.destroy()


class _PrefixDialog(tk.Toplevel):
    """Small modal to choose the in-archive path prefix for added files."""
    def __init__(self, parent, default=""):
        super().__init__(parent)
        self.result = None
        self.title("Destination inside archive")
        self.configure(bg="#272d36")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="Folder inside the archive (blank = root):",
                  style="Sub.TLabel").pack(padx=16, pady=(16, 6), anchor="w")
        self.var = tk.StringVar(value=default)
        ent = tk.Entry(self, textvariable=self.var, width=46, bg="#1f242b",
                       fg="#dfe5ec", insertbackground="#dfe5ec", relief="flat",
                       highlightthickness=1, highlightbackground="#36404b",
                       highlightcolor="#4ea1d3")
        ent.pack(padx=16, ipady=4, fill="x")
        ent.focus_set()
        ent.icursor("end")

        btns = ttk.Frame(self)
        btns.pack(padx=16, pady=14, fill="x")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="OK", style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        parent.update_idletasks()
        x = parent.winfo_rootx() + parent.winfo_width() // 2 - 200
        y = parent.winfo_rooty() + parent.winfo_height() // 2 - 60
        self.geometry(f"+{max(x,0)}+{max(y,0)}")

    def _ok(self):
        p = self.var.get().strip().replace("\\", "/")
        if p and not p.endswith("/"):
            p += "/"
        self.result = p
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class _MethodDialog(tk.Toplevel):
    """Choose the compression method for saving / packing.

    result = (method_const, preserve_bool) or None if cancelled.
    """
    def __init__(self, parent, allow_preserve=True):
        super().__init__(parent)
        self.result = None
        self.title("Compression method")
        self.configure(bg="#272d36")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="How should the archive be packed?",
                  style="Head.TLabel").pack(padx=18, pady=(16, 4), anchor="w")
        ttk.Label(self,
                  text="The Xbox 360 game requires LZMA — Store/Deflate crash it.",
                  style="Sub.TLabel").pack(padx=18, pady=(0, 12), anchor="w")

        self.method_var = tk.IntVar(value=METHOD_LZMA_X360)
        self.preserve_var = tk.BooleanVar(value=allow_preserve)

        def radio(value, title, desc):
            row = ttk.Frame(self)
            row.pack(fill="x", padx=18, pady=3)
            rb = tk.Radiobutton(
                row, variable=self.method_var, value=value, text=title,
                bg="#272d36", fg="#dfe5ec", selectcolor="#1f242b",
                activebackground="#272d36", activeforeground="#ffffff",
                font=("Segoe UI Semibold", 10), anchor="w",
                command=self._sync)
            rb.pack(anchor="w")
            ttk.Label(row, text="    " + desc, style="Sub.TLabel").pack(anchor="w")

        radio(METHOD_LZMA_X360, "LZMA — Xbox 360  (recommended)",
              "Method 24. What the console build needs. "
              + ("pylzma active." if _HAVE_PYLZMA else "using stdlib lzma."))
        radio(METHOD_DEFLATE, "Deflate  (standard ZIP)",
              "Smaller, opens in any ZIP tool. For PC use, not 360.")
        radio(METHOD_STORE, "Store  (uncompressed)",
              "No compression; largest file, fastest to pack.")

        self.preserve_cb = tk.Checkbutton(
            self, variable=self.preserve_var,
            text="Keep each unchanged file's original method",
            bg="#272d36", fg="#dfe5ec", selectcolor="#1f242b",
            activebackground="#272d36", activeforeground="#ffffff",
            font=("Segoe UI", 9), anchor="w")
        if allow_preserve:
            self.preserve_cb.pack(anchor="w", padx=16, pady=(12, 0))
        self._sync()

        btns = ttk.Frame(self)
        btns.pack(padx=18, pady=16, fill="x")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        parent.update_idletasks()
        x = parent.winfo_rootx() + parent.winfo_width() // 2 - 230
        y = parent.winfo_rooty() + parent.winfo_height() // 2 - 140
        self.geometry(f"+{max(x,0)}+{max(y,0)}")

    def _sync(self):
        # "preserve original method" only makes sense when packing as LZMA;
        # otherwise the chosen method applies uniformly.
        if self.method_var.get() == METHOD_LZMA_X360:
            self.preserve_cb.config(state="normal")
        else:
            self.preserve_var.set(False)
            self.preserve_cb.config(state="disabled")

    def _ok(self):
        self.result = (self.method_var.get(), bool(self.preserve_var.get()))
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def main():
    app = ScdEditorApp()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.after(100, lambda: app._load(sys.argv[1]))
    app.mainloop()


if __name__ == "__main__":
    main()
