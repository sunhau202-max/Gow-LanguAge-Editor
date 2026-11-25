#!/usr/bin/env python3
"""
Gow LanguAge Editor  (God of War 1 & 2, PS2)

Mengedit / melihat bahasa (teks) God of War 1 dan 2 yang ada di dalam file WAD:
  - GoW 1: msgs_en.txt (di R_PERM.WAD)      - GoW 2: MSGS_TXT (di R_PERMA.WAD)  -> teks bisa diedit
  - FLP_* (skrip UI Flash)                  -> string bisa dilihat; ganti hanya dengan panjang SAMA
Sumber: ISO, satu file WAD, atau folder WAD.  SAVE menulis ke SEMUA salinan WAD di ISO dengan backup
(NAMA.iso.langbak) dan tombol Kembalikan ISO.  Hanya butuh Python 3.8+ (tkinter).  Jalan di PC dan Wine.
"""
import os, re, sys, json, base64, struct, traceback, threading, zlib

SECTOR = 2048
LOG = []


def log(*a):
    LOG.append(" ".join(str(x) for x in a))


# ======================================================================
# ISO9660
# ======================================================================
class ISO:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.ssz = self.off = None
        for ssz, off in ((2048, 0), (2352, 16), (2336, 8)):
            self.f.seek(16 * ssz + off)
            if self.f.read(6)[1:6] == b"CD001":
                self.ssz, self.off = ssz, off
                break
        if self.ssz is None:
            raise ValueError("Bukan ISO9660 yang valid")
        self.files = {}
        pvd = self.sectors(16, 1)
        root = pvd[156:190]
        self._walk(struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0], "")

    def sectors(self, lba, count):
        if self.ssz == 2048:
            self.f.seek(lba * 2048)
            return self.f.read(count * 2048)
        out = []
        for i in range(count):
            self.f.seek((lba + i) * self.ssz + self.off)
            out.append(self.f.read(2048))
        return b"".join(out)

    def _walk(self, lba, size, prefix):
        data = self.sectors(lba, (size + 2047) // 2048)
        p = 0
        while p < len(data):
            ln = data[p]
            if ln == 0:
                p = (p // 2048 + 1) * 2048
                continue
            rec = data[p:p + ln]
            p += ln
            elba, esize = struct.unpack("<I", rec[2:6])[0], struct.unpack("<I", rec[10:14])[0]
            flags, nlen = rec[25], rec[32]
            name = rec[33:33 + nlen]
            if name in (b"\x00", b"\x01"):
                continue
            name = name.decode("ascii", "replace").split(";")[0]
            if flags & 2:
                self._walk(elba, esize, prefix + name + "/")
            else:
                self.files[(prefix + name).upper()] = (elba, esize)

    # --- antarmuka "Disc"
    def find(self, name):
        for k, v in self.files.items():
            if k.split("/")[-1] == name.upper():
                return v
        return None

    def toc_bytes(self):
        v = self.find("GODOFWAR.TOC")
        return self.read_range(v[0], 0, v[1]) if v else None

    def paks(self):
        r = {}
        for k, v in self.files.items():
            n = k.split("/")[-1]
            if re.fullmatch(r"PART\d+\.PAK", n):
                r[n] = v[1]
        return r

    def read_pak(self, pak, offset, length):
        v = self.find(pak)
        if not v or offset < 0 or offset >= v[1]:
            return b""
        return self.read_range(v[0], offset, min(length, v[1] - offset))

    def write_pak(self, pak, offset, data):
        """Tulis langsung ke ISO (hanya ISO 2048-byte/sektor biasa). Ukuran data tidak berubah."""
        if self.ssz != 2048:
            raise ValueError("Replace hanya didukung untuk ISO 2048 byte/sektor (bukan .bin raw)")
        v = self.find(pak)
        if not v or offset + len(data) > v[1]:
            raise ValueError("Lokasi tulis di luar PAK")
        with open(self.path, "r+b") as w:
            w.seek(v[0] * 2048 + offset)
            w.write(data)
        self.f.close()
        self.f = open(self.path, "rb")

    def read_range(self, lba, offset, length):
        first = offset // 2048
        last = (offset + length + 2047) // 2048
        buf = self.sectors(lba + first, last - first)
        s = offset - first * 2048
        return buf[s:s + length]

    def scan_pak(self, pak, magic_tags, progress=None):
        """Cari semua sektor yang diawali header WAD (tag, size, 'WAD_...'). Return list offset byte."""
        v = self.find(pak)
        hits = []
        if not v:
            return hits
        CH = 64 * 1024 * 1024
        pos = 0
        while pos < v[1]:
            chunk = self.read_range(v[0], pos, min(CH, v[1] - pos))
            i = chunk.find(b"WAD_")
            while i != -1:
                s = i - 8
                if s >= 0 and s % 2048 == 0 and struct.unpack_from("<I", chunk, s)[0] in magic_tags:
                    hits.append(pos + s)
                i = chunk.find(b"WAD_", i + 1)
            pos += CH
            if progress:
                progress("Scan %s: %d%%" % (pak, min(100, pos * 100 // v[1])))
        return hits


# ======================================================================
# TOC
# ======================================================================
class TocEntry:
    """Satu file di TOC. locs = daftar salinan [(PARTn.PAK, offset_byte)] (file bisa ada di 2 PAK)."""
    __slots__ = ("name", "size", "locs")

    def __init__(self, name, size):
        self.name, self.size, self.locs = name, size, []


PAK2_BASE = 10000000  # GoW2: nilai offset >= 10.000.000 berarti PART2.PAK (sektor = nilai - 10.000.000)


def parse_toc(d):
    """Return (game, entries). game = 1 atau 2.
    GoW2 : u32 jumlah | entry 0x24 {name[24], size, jumlah_salinan, index} | tabel u32 offset-sektor
    GoW1 : entry 0x18 {name[12], u32 pak(0=PART1,1=PART2), size, offset-sektor}"""
    byname = {}
    if len(d) >= 4:
        n = struct.unpack_from("<I", d, 0)[0]
        tab = 4 + n * 0x24
        if 0 < n < 20000 and tab < len(d) and (len(d) - tab) % 4 == 0:
            T = struct.unpack_from("<%dI" % ((len(d) - tab) // 4), d, tab)
            for i in range(n):
                o = 4 + i * 0x24
                nm = d[o:o + 24].split(b"\0")[0].decode("latin1")
                size, copies, idx = struct.unpack_from("<3I", d, o + 24)
                e = byname.setdefault(nm, TocEntry(nm, size))
                for k in range(copies):
                    if idx + k < len(T):
                        v = T[idx + k]
                        e.locs.append(("PART2.PAK", (v - PAK2_BASE) * SECTOR) if v >= PAK2_BASE
                                      else ("PART1.PAK", v * SECTOR))
            return 2, list(byname.values())
    for i in range(len(d) // 24):
        o = i * 24
        nm = d[o:o + 12].split(b"\0")[0].decode("latin1")
        if not nm:
            continue
        pk, size, off = struct.unpack_from("<3I", d, o + 12)
        e = byname.setdefault(nm, TocEntry(nm, size))
        e.locs.append(("PART%d.PAK" % (pk + 1), off * SECTOR))
    return 1, list(byname.values())


def wad_magic_ok(h):
    return len(h) >= 12 and h[8:12] == b"WAD_"


def verify_toc(ents, disc):
    """Cek sampel: header WAD ('WAD_') harus ada di lokasi hasil TOC. Hasil dicatat di log."""
    wads = [e for e in ents if e.name.upper().endswith(".WAD") and e.locs]
    sample = wads[::max(1, len(wads) // 30)][:30]
    ok = 0
    for e in sample:
        pk, off = e.locs[0]
        if wad_magic_ok(disc.read_pak(pk, off, 32)):
            ok += 1
        else:
            log("lokasi tidak cocok:", e.name, e.locs)
    log("verifikasi TOC: %d/%d WAD sampel valid" % (ok, len(sample)))
    return ok, len(sample)


# ======================================================================
# WAD
# ======================================================================
KNOWN_TAGS = {0x1e, 0x28, 0x32, 0x70, 0x71, 0x72, 0x1001e, 0x3001e, 0x4001e, 0x6001e, 0x7001e, 0x8001e,
              0x2001e, 0x14001e, 0x16001e, 0x19001e, 0x378, 0x29a, 0x3e7,
              1, 2, 3, 9, 0x13, 0x15, 0x16, 0x10001, 0x30001, 0x40001, 0x50001, 0x60001, 0x70001, 0x80001,
              0x170001, 0x1a0001, 0x1d0001}


def _hdr(d, p):
    if p + 0x20 > len(d):
        return None
    tag, size = struct.unpack_from("<II", d, p)
    raw = d[p + 8:p + 0x20]
    nm = raw.split(b"\0")[0]
    if any(c < 32 or c >= 127 for c in nm) or raw[len(nm):].strip(b"\0"):
        return None
    return tag, size, nm.decode("latin1")




# ======================================================================
# WAD: pembaca entri (versi bahasa: entri bertag 0 = variabel, nilainya di field "size", tanpa data)
# ======================================================================
def parse_wad(d):
    out, p, n, skips = [], 0, len(d), 0
    while p + 0x20 <= n:
        h = _hdr(d, p)
        if h and h[2]:
            tag, size, name = h
            data = 0 if tag == 0 else size
            if p + 0x20 + data <= n and not (name.startswith("SND_") and tag == 0x18):
                out.append({"off": p + 0x20, "tag": tag, "size": size, "name": name, "data": data})
                p += 0x20 + ((data + 15) & ~15)
                continue
        skips += 1
        q = p + 0x10
        while q + 0x20 <= n:
            hh = _hdr(d, q)
            if hh and hh[2] and hh[0] in KNOWN_TAGS and q + 0x20 + (0 if hh[0] == 0 else hh[1]) <= n:
                break
            q += 0x10
        p = q
    return out


# ======================================================================
# Teks bahasa
# ======================================================================
MSG_NAME = re.compile(r"^(msgs_.+\.txt|MSGS_TXT)$", re.I)       # GoW1: msgs_en.txt   GoW2: MSGS_TXT
MARK = re.compile(r"(?m)^\*(\d+)\*\n")


def entry_kind(e):
    """'msgs' = teks bahasa (bisa diedit), 'flp' = skrip UI Flash (string saja), None = bukan teks."""
    if MSG_NAME.match(e["name"]) and e["data"] > 16:
        return "msgs"
    if e["name"].upper().startswith("FLP_") and e["data"] > 64:
        return "flp"
    return None


class MsgDoc:
    """Daftar pesan '*N*\\nteks' dengan format asli dipertahankan (jeda baris, terminator)."""

    def __init__(self, raw):
        text = raw.rstrip(b"\0").decode("latin-1")
        ms = list(MARK.finditer(text))
        self.pre = text[:ms[0].start()] if ms else text
        self.items = []
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            body = text[m.end():end]
            s = body.rstrip("\n")
            self.items.append({"id": int(m.group(1)), "text": s, "trail": body[len(s):], "orig": s})

    def build(self):
        out = self.pre + "".join("*%d*\n%s%s" % (it["id"], it["text"], it["trail"]) for it in self.items)
        return out.encode("latin-1") + b"\0"

    def changed(self):
        return [it["id"] for it in self.items if it["text"] != it["orig"]]

    def export_text(self):
        return (self.pre + "".join("*%d*\n%s%s" % (it["id"], it["text"], it["trail"]) for it in self.items)).encode("latin-1")


def flp_strings(raw, minlen=4):
    """String ASCII di dalam FLP: [(offset, bytes)] (hanya yang mengandung huruf)."""
    out = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, raw):
        if re.search(rb"[A-Za-z]", m.group(0)):
            out.append((m.start(), m.group(0)))
    return out


class TooLong(ValueError):
    def __init__(self, need, have):
        super().__init__("Teks terlalu panjang: butuh %d byte, jatah %d byte." % (need, have))
        self.need, self.have = need, have


def patch_entry(wad, ent, new_data, allow_grow=False):
    """Tulis data baru ke satu entri WAD.
    - muat di ukuran asli          -> sisa diisi 0, header tidak berubah
    - muat di perataan 16 byte      -> field size di header diperbarui (entri berikutnya tidak bergeser)
    - lebih besar & allow_grow      -> WAD diperbesar (entri berikutnya digeser) -- hanya untuk WAD lepas
    Return (wad_baru, catatan)."""
    out = bytearray(wad)
    off, size = ent["off"], ent["size"]
    aligned = (size + 15) & ~15
    n = len(new_data)
    if n <= size:
        out[off:off + aligned] = new_data + b"\0" * (aligned - n)
        return out, "ukuran tetap (%d/%d byte)" % (n, size)
    if n <= aligned:
        struct.pack_into("<I", out, off - 0x20 + 4, n)
        out[off:off + aligned] = new_data + b"\0" * (aligned - n)
        return out, "ukuran entri naik %d -> %d (dalam perataan 16 byte)" % (size, n)
    if not allow_grow:
        raise TooLong(n, aligned)
    new_al = (n + 15) & ~15
    head = bytearray(out[off - 0x20:off])
    struct.pack_into("<I", head, 4, n)
    out = bytearray(out[:off - 0x20]) + head + new_data + b"\0" * (new_al - n) + bytearray(wad[off + aligned:])
    return out, "WAD diperbesar %d byte" % (len(out) - len(wad))


def app_dir():
    base = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    return os.path.dirname(base)


ERR_FILE = "gow_language_editor_error.txt"


def log_error(text):
    """Catat error ke file di samping program (seperti tool SBK)."""
    try:
        with open(os.path.join(app_dir(), ERR_FILE), "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 60 + "\n")
    except Exception:
        pass
    return os.path.join(app_dir(), ERR_FILE)



# ======================================================================
# Backup (isi asli sebelum ditimpa)
# ======================================================================
BK_MAGIC = b"GOWLBK1\n"


def bk_read(path):
    recs = []
    if not os.path.exists(path):
        return recs
    with open(path, "rb") as f:
        if f.read(8) != BK_MAGIC:
            return recs
        while True:
            h = f.read(4)
            if len(h) < 4:
                break
            jl = struct.unpack("<I", h)[0]
            meta = json.loads(f.read(jl).decode())
            recs.append((meta["pak"], meta["off"], f.read(meta["len"])))
    return recs


def bk_add(path, pak, off, data):
    """Simpan isi ASLI sebelum ditimpa. Lokasi yang sudah pernah dibackup tidak ditimpa lagi."""
    have = {(p, o, len(d)) for p, o, d in bk_read(path)}
    if (pak, off, len(data)) in have:
        return
    new = not os.path.exists(path)
    with open(path, "ab") as f:
        if new:
            f.write(BK_MAGIC)
        meta = json.dumps({"pak": pak, "off": off, "len": len(data)}).encode()
        f.write(struct.pack("<I", len(meta)) + meta + data)


def wad_game(d):
    """GoW1: entry pertama tag 0x378; GoW2: tag 0x15."""
    if len(d) >= 12 and d[8:12] == b"WAD_":
        return 1 if struct.unpack_from("<I", d, 0)[0] == 0x378 else 2
    return None



def settings_path():
    return os.path.join(app_dir(), "gow_language_editor_settings.json")


def load_settings():
    try:
        return json.load(open(settings_path(), encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d):
    try:
        json.dump(d, open(settings_path(), "w", encoding="utf-8"))
    except Exception:
        pass




# ======================================================================
# Katalog WAD yang berisi teks (hasil pemindaian semua WAD GoW 1 & GoW 2 yang diberikan)
# ======================================================================
CATALOG_B64 = (
    "eNp10k9vgjAYBvDv0jMxbd++BbxVYHJAR8TFJcYQnG4e8E8mhyXG7z46Rx22O1J+fZ+nlAsZHxeMDC9EzdMpZaPBQsVkuFySpywvVbM7tGvEI+"
    "/1iXhcSMFXK+/XRg4b/WMTh006C4KFaGygerbSNqg6G1DO7nTkoOuOShHIO40c9M205QEDbXM1jSmMezavDpt2zdiwravtrMyT2aRH05e4Y8IP"
    "gcLKW5L9+eNcbg+D5qtpX+qn9sgSEW5DijTJst6UYret624OkzL02y959fRVcX1Vs/Q5Btrb8rk7boB2e9AXPLhXVI8dlSlJfUShS06KcVHOX+"
    "emIVKU4Z+Gyq5oxjCgIfU1jiajOWc9Gu3XDWcmkd9+ix/IbcidEGwILgh2NDijwY4GZzTY0eCMFna0MNEohDDQjhbcCe1oAS6IdjSaaCl90xHt"
    "aOROaEcjPMJcZRx77lTVHPsNr9dvjYQshQ=="
)
_CAT = None


# ======================================================================
# Sumber: ISO / satu WAD / folder WAD  +  perubahan tertunda (SAVE)
# ======================================================================
class WadRef:
    def __init__(self, name, size, game, locs=None, path=None):
        self.name, self.size, self.game = name, size, game
        self.locs = locs or []      # ISO: [(PARTn.PAK, offset)]
        self.path = path            # WAD lepas: path file


def catalog():
    global _CAT
    if _CAT is None:
        try:
            _CAT = json.loads(zlib.decompress(base64.b64decode(CATALOG_B64)))
        except Exception:
            _CAT = {"GoW1": {}, "GoW2": {}}
    return _CAT


class Session:
    """kind: 'iso' | 'wad' | 'folder'."""

    def __init__(self, kind, disc=None, path=None):
        self.kind, self.disc, self.path = kind, disc, path
        self.refs, self.game = [], None
        self.pending, self.wad_cache, self.ent_cache, self.extra = {}, {}, {}, {}
        if kind == "iso":
            toc = disc.toc_bytes()
            if not toc:
                raise ValueError("GODOFWAR.TOC tidak ada di ISO")
            self.game, ents = parse_toc(toc)
            log("Game terdeteksi: GoW%d, file di TOC: %d" % (self.game, len(ents)))
            ok, tot = verify_toc(ents, disc)
            self.method = "TOC OK %d/%d" % (ok, tot)
            self.refs = [WadRef(e.name, e.size, self.game, e.locs) for e in ents
                         if e.name.upper().endswith(".WAD") and e.locs]
            self.backup_path = disc.path + ".langbak"
        else:
            files = [path] if kind == "wad" else sorted(
                os.path.join(path, f) for f in os.listdir(path) if f.upper().endswith(".WAD"))
            for fp in files:
                with open(fp, "rb") as f:
                    g = wad_game(f.read(32))
                if g:
                    self.refs.append(WadRef(os.path.basename(fp).upper(), os.path.getsize(fp), g, path=fp))
            self.game = self.refs[0].game if self.refs else None
            self.method = "%d WAD" % len(self.refs)
            self.backup_path = None
        self.by_name = {r.name: r for r in self.refs}

    # ---- daftar
    def pak_names(self):
        return sorted(self.disc.paks()) if self.kind == "iso" else []

    def known(self, ref):
        """Entri teks WAD ini menurut katalog / hasil pindai (None = belum diketahui)."""
        k = "GoW%d" % ref.game
        if ref.name.upper() in self.extra:
            return self.extra[ref.name.upper()]
        return catalog().get(k, {}).get(ref.name.upper())

    def wads_in(self, pak=None):
        """WAD yang punya teks. ISO: dari katalog + hasil 'Pindai semua'. WAD lepas: dipindai langsung."""
        out = []
        for r in sorted(self.refs, key=lambda x: x.name):
            if pak is not None and not any(l[0] == pak for l in r.locs):
                continue
            if self.kind != "iso":
                if self.text_entries(r, None):
                    out.append(r)
            elif self.known(r):
                out.append(r)
        return out

    def scan_all(self, progress=None):
        """ISO: baca SEMUA WAD untuk mencari teks (lebih lama). Return jumlah WAD baru yang punya teks."""
        found = 0
        for i, r in enumerate(sorted(self.refs, key=lambda x: x.name)):
            if progress:
                progress(i + 1, len(self.refs), r.name)
            if r.name.upper() in self.extra:
                continue
            pak = r.locs[0][0] if r.locs else None
            ents = [[e["name"], entry_kind(e), e["size"]] for e in parse_wad(self.wad_data(r, pak)) if entry_kind(e)]
            self.extra[r.name.upper()] = ents
            if ents and not catalog().get("GoW%d" % r.game, {}).get(r.name.upper()):
                found += 1
        return found

    # ---- baca
    def wad_data(self, ref, pak=None):
        if ref.name in self.pending:
            return bytes(self.pending[ref.name])
        k = (ref.name, pak)
        if k not in self.wad_cache:
            if len(self.wad_cache) > 4:
                self.wad_cache.pop(next(iter(self.wad_cache)))
            if ref.path:
                self.wad_cache[k] = open(ref.path, "rb").read()
            else:
                loc = next((l for l in ref.locs if pak is None or l[0] == pak), ref.locs[0])
                self.wad_cache[k] = self.disc.read_pak(loc[0], loc[1], ref.size)
        return self.wad_cache[k]

    def text_entries(self, ref, pak=None):
        d = self.wad_data(ref, pak)
        if not wad_magic_ok(d[:32]):
            log("WAD tidak valid di lokasi:", ref.name, ref.locs or ref.path)
            return []
        out = []
        for e in parse_wad(d):
            k = entry_kind(e)
            if k:
                e = dict(e)
                e["kind"] = k
                out.append(e)
        return out

    def entry_raw(self, ref, pak, name):
        d = self.wad_data(ref, pak)
        e = next(x for x in parse_wad(d) if x["name"] == name)
        return d[e["off"]:e["off"] + e["data"]], e

    # ---- tulis (di memori; ditulis ke ISO/file oleh save())
    def apply_entry(self, ref, pak, name, new_data, allow_grow=None):
        if allow_grow is None:
            allow_grow = self.kind != "iso"
        d = self.wad_data(ref, pak)
        e = next(x for x in parse_wad(d) if x["name"] == name)
        new, note = patch_entry(d, e, new_data, allow_grow)
        if self.kind == "iso" and len(new) != len(d):
            raise TooLong(len(new_data), (e["size"] + 15) & ~15)
        self.pending[ref.name] = bytearray(new)
        return note

    def has_pending(self):
        return bool(self.pending)

    def discard(self):
        self.pending.clear()
        self.wad_cache.clear()

    def save(self):
        if not self.pending:
            return []
        lines = []
        if self.kind == "iso":
            for name in self.pending:
                for pk, off in self.by_name[name].locs:
                    if not wad_magic_ok(self.disc.read_pak(pk, off, 32)):
                        raise ValueError("Penanda WAD tidak cocok di %s @%d (%s). Tidak ada yang ditulis." % (pk, off, name))
            for name, data in self.pending.items():
                ref = self.by_name[name]
                for pk, off in ref.locs:
                    bk_add(self.backup_path, pk, off, self.disc.read_pak(pk, off, ref.size))
                    self.disc.write_pak(pk, off, bytes(data))
                lines.append("%s -> %d lokasi di ISO" % (name, len(ref.locs)))
            lines.append("Backup: " + self.backup_path)
        else:
            for name, data in self.pending.items():
                fp = self.by_name[name].path
                if not os.path.exists(fp + ".bak"):
                    with open(fp, "rb") as f, open(fp + ".bak", "wb") as b:
                        b.write(f.read())
                with open(fp, "wb") as f:
                    f.write(bytes(data))
                lines.append("%s ditulis (backup %s.bak)" % (name, os.path.basename(fp)))
        self.pending.clear()
        self.wad_cache.clear()
        return lines

    def save_as(self, ref, outpath, pak=None):
        open(outpath, "wb").write(self.wad_data(ref, pak))

    def restore(self):
        if self.kind != "iso":
            raise ValueError("Kembalikan hanya untuk ISO")
        recs = bk_read(self.backup_path)
        if not recs:
            raise ValueError("Tidak ada backup (%s)" % self.backup_path)
        for pk, off, data in recs:
            self.disc.write_pak(pk, off, data)
        self.wad_cache.clear()
        return len(recs)


def open_source(path):
    path = path.strip().strip('"')
    if os.path.isdir(path):
        return Session("folder", path=path)
    if os.path.splitext(path)[1].lower() == ".wad":
        return Session("wad", path=path)
    return Session("iso", disc=ISO(path), path=path)


# ======================================================================
# Bahasa tampilan (Indonesia / English / Português)
# ======================================================================
TXT = {
    "id": {
        "open_iso": "Buka ISO", "open_wad": "Buka WAD", "open_folder": "Buka folder WAD", "open_path": "Buka path",
        "scan_all": "Pindai semua WAD", "catalog": "Katalog", "save": "SAVE", "save_as": "Simpan sebagai",
        "restore": "Kembalikan ISO", "discard": "Batalkan perubahan", "dump": "Dump Log", "lang": "Bahasa",
        "path": "Path ISO / WAD / folder:", "none": "Belum ada sumber dibuka", "reading": "Membaca...",
        "search": "Cari:", "apply": "Terapkan", "revert": "Kembalikan teks ini", "export": "Ekspor teks",
        "import": "Impor teks", "budget": "Terpakai %d / jatah %d byte", "budget_free": "Terpakai %d byte (WAD lepas: boleh diperbesar)",
        "flp_note": "FLP = skrip UI. Ganti string hanya dengan panjang SAMA (%d byte); salah ganti bisa merusak menu.",
        "pick_entry": "Pilih satu entri teks (msgs / FLP) di daftar kiri dulu.", "pick_wad": "Pilih satu WAD dulu.",
        "applied": "Diterapkan (belum ditulis, klik SAVE):\n%s", "too_long": "%s\n\nUntuk ISO ukuran WAD tidak boleh berubah. Persingkat teksnya.",
        "grow_ask": "%s\n\nPerbesar WAD? (hanya untuk WAD lepas)", "bad_char": "Ada karakter yang tidak didukung (hanya Latin-1): %s",
        "len_diff": "Panjang harus SAMA: asli %d byte, baru %d byte.", "saved": "Selesai:\n%s", "restored": "ISO dikembalikan (%d lokasi).",
        "discarded": "Perubahan dibatalkan.", "no_pending": "Belum ada perubahan.", "confirm_save": "Tulis %d WAD yang diubah?\nKerjakan di SALINAN. Isi asli di-backup.",
        "err": "Error", "err_where": "Detail error ada di:\n%s", "log_done": "Ditulis ke gow_debug.log (kirim ke pembuat)",
        "cat_title": "Katalog WAD berisi teks", "open_this": "Buka WAD ini", "cat_open_first": "Buka ISO/folder dulu.",
        "not_found": "WAD itu tidak ada di sumber yang terbuka.", "unsaved": "Ada perubahan belum di-SAVE. Keluar?",
        "pick_lang": "Pilih bahasa / Choose language", "files": "File teks", "all": "Semua", "dest": "Simpan ke",
        "exported": "Teks diekspor ke:\n%s", "imported": "Teks diimpor: %d pesan.", "scanned": "Pemindaian selesai: %d WAD baru berisi teks.",
        "scanning": "Memindai %d/%d  %s", "msgs_n": "%s  [teks bahasa, %d pesan]", "flp_n": "%s  [FLP, lihat string]", "wad_n": "%s",
        "changed": "diubah", "id_col": "ID", "text_col": "Teks", "off_col": "Offset",
    },
    "en": {
        "open_iso": "Open ISO", "open_wad": "Open WAD", "open_folder": "Open WAD folder", "open_path": "Open path",
        "scan_all": "Scan all WADs", "catalog": "Catalog", "save": "SAVE", "save_as": "Save as",
        "restore": "Restore ISO", "discard": "Discard changes", "dump": "Dump Log", "lang": "Language",
        "path": "ISO / WAD / folder path:", "none": "No source opened", "reading": "Reading...",
        "search": "Search:", "apply": "Apply", "revert": "Revert this text", "export": "Export text",
        "import": "Import text", "budget": "Used %d / allowed %d bytes", "budget_free": "Used %d bytes (loose WAD: may grow)",
        "flp_note": "FLP = UI script. Replace a string only with the SAME length (%d bytes); wrong edits can break menus.",
        "pick_entry": "Select a text entry (msgs / FLP) in the left list first.", "pick_wad": "Select a WAD first.",
        "applied": "Applied (not written yet, click SAVE):\n%s", "too_long": "%s\n\nIn an ISO the WAD size cannot change. Shorten the text.",
        "grow_ask": "%s\n\nEnlarge the WAD? (loose WAD only)", "bad_char": "Unsupported character (Latin-1 only): %s",
        "len_diff": "Length must be the SAME: original %d bytes, new %d bytes.", "saved": "Done:\n%s", "restored": "ISO restored (%d locations).",
        "discarded": "Changes discarded.", "no_pending": "No changes yet.", "confirm_save": "Write %d modified WAD(s)?\nWork on a COPY. Originals are backed up.",
        "err": "Error", "err_where": "Error details are in:\n%s", "log_done": "Written to gow_debug.log (send it to the author)",
        "cat_title": "WADs that contain text", "open_this": "Open this WAD", "cat_open_first": "Open an ISO/folder first.",
        "not_found": "That WAD is not in the opened source.", "unsaved": "There are unsaved changes. Quit?",
        "pick_lang": "Pilih bahasa / Choose language", "files": "Text files", "all": "All", "dest": "Save to",
        "exported": "Text exported to:\n%s", "imported": "Text imported: %d messages.", "scanned": "Scan finished: %d new WADs with text.",
        "scanning": "Scanning %d/%d  %s", "msgs_n": "%s  [language text, %d messages]", "flp_n": "%s  [FLP, view strings]", "wad_n": "%s",
        "changed": "changed", "id_col": "ID", "text_col": "Text", "off_col": "Offset",
    },
    "pt": {
        "open_iso": "Abrir ISO", "open_wad": "Abrir WAD", "open_folder": "Abrir pasta WAD", "open_path": "Abrir caminho",
        "scan_all": "Varrer todos os WADs", "catalog": "Catálogo", "save": "SALVAR", "save_as": "Salvar como",
        "restore": "Restaurar ISO", "discard": "Descartar alterações", "dump": "Dump Log", "lang": "Idioma",
        "path": "Caminho ISO / WAD / pasta:", "none": "Nenhuma fonte aberta", "reading": "Lendo...",
        "search": "Buscar:", "apply": "Aplicar", "revert": "Desfazer este texto", "export": "Exportar texto",
        "import": "Importar texto", "budget": "Usado %d / permitido %d bytes", "budget_free": "Usado %d bytes (WAD solto: pode crescer)",
        "flp_note": "FLP = script de UI. Troque uma string só por outra do MESMO tamanho (%d bytes); erros podem quebrar menus.",
        "pick_entry": "Selecione uma entrada de texto (msgs / FLP) na lista à esquerda.", "pick_wad": "Selecione um WAD.",
        "applied": "Aplicado (ainda não gravado, clique em SALVAR):\n%s", "too_long": "%s\n\nNa ISO o tamanho do WAD não pode mudar. Encurte o texto.",
        "grow_ask": "%s\n\nAumentar o WAD? (somente WAD solto)", "bad_char": "Caractere não suportado (apenas Latin-1): %s",
        "len_diff": "O tamanho deve ser IGUAL: original %d bytes, novo %d bytes.", "saved": "Concluído:\n%s", "restored": "ISO restaurada (%d locais).",
        "discarded": "Alterações descartadas.", "no_pending": "Sem alterações.", "confirm_save": "Gravar %d WAD(s) alterado(s)?\nTrabalhe em uma CÓPIA. Os originais ficam em backup.",
        "err": "Erro", "err_where": "Detalhes do erro em:\n%s", "log_done": "Gravado em gow_debug.log (envie ao autor)",
        "cat_title": "WADs com texto", "open_this": "Abrir este WAD", "cat_open_first": "Abra uma ISO/pasta primeiro.",
        "not_found": "Esse WAD não está na fonte aberta.", "unsaved": "Há alterações não salvas. Sair?",
        "pick_lang": "Pilih bahasa / Choose language", "files": "Arquivos de texto", "all": "Todos", "dest": "Salvar em",
        "exported": "Texto exportado para:\n%s", "imported": "Texto importado: %d mensagens.", "scanned": "Varredura concluída: %d novos WADs com texto.",
        "scanning": "Varrendo %d/%d  %s", "msgs_n": "%s  [texto do idioma, %d mensagens]", "flp_n": "%s  [FLP, ver strings]", "wad_n": "%s",
        "changed": "alterado", "id_col": "ID", "text_col": "Texto", "off_col": "Offset",
    },
}
LANG_NAMES = {"id": "Indonesia", "en": "English", "pt": "Português"}


# ======================================================================
# GUI
# ======================================================================
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Gow LanguAge Editor")
            self.geometry("1000x640")
            self.s = None
            self.meta = {}
            self.cur = None            # entri yang sedang dibuka
            self.rows = {}             # baris daftar -> indeks item
            self.st = load_settings()
            self.lang = self.st.get("lang") if self.st.get("lang") in TXT else None
            self.widgets = {}
            self.report_callback_exception = self.on_exc
            if not self.lang:
                self.lang = self.choose_lang()
                self.st["lang"] = self.lang
                save_settings(self.st)
            self.build()
            self.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- util
        def t(self, k, *a):
            s = TXT[self.lang][k]
            return s % a if a else s

        def choose_lang(self):
            win = tk.Toplevel(self)
            win.title("Language")
            var = tk.StringVar(value="id")
            ttk.Label(win, text=TXT["id"]["pick_lang"]).pack(padx=12, pady=8)
            for code, nm in LANG_NAMES.items():
                ttk.Radiobutton(win, text=nm, value=code, variable=var).pack(anchor="w", padx=20)
            ttk.Button(win, text="OK", command=win.destroy).pack(pady=8)
            win.grab_set()
            self.wait_window(win)
            return var.get()

        def on_exc(self, exc, val, tb):
            txt = "".join(traceback.format_exception(exc, val, tb))
            log(txt)
            messagebox.showerror(self.t("err"), "%s\n\n%s" % (val, self.t("err_where", log_error(txt))))

        def fail(self, e):
            txt = traceback.format_exc()
            log(txt)
            messagebox.showerror(self.t("err"), "%s\n\n%s" % (e, self.t("err_where", log_error(txt))))

        def say(self, text):
            self.after(0, lambda: self.status.config(text=text))

        # ---- UI
        def add_btn(self, parent, key, cmd):
            b = ttk.Button(parent, command=cmd)
            b.pack(side="left", padx=2)
            self.widgets[key] = b

        def build(self):
            top = ttk.Frame(self)
            top.pack(fill="x", padx=6, pady=(6, 2))
            self.path_lbl = ttk.Label(top)
            self.path_lbl.pack(side="left")
            self.path_var = tk.StringVar()
            ttk.Entry(top, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=6)
            self.add_btn(top, "open_path", self.open_path)
            r1 = ttk.Frame(self)
            r1.pack(fill="x", padx=6, pady=2)
            for k, c in (("open_iso", self.open_iso), ("open_wad", self.open_wad), ("open_folder", self.open_folder),
                         ("scan_all", self.scan_all), ("catalog", self.show_catalog)):
                self.add_btn(r1, k, c)
            r2 = ttk.Frame(self)
            r2.pack(fill="x", padx=6, pady=2)
            for k, c in (("save", self.do_save), ("save_as", self.do_save_as), ("restore", self.do_restore),
                         ("discard", self.do_discard), ("dump", self.dump_log)):
                self.add_btn(r2, k, c)
            self.lang_lbl = ttk.Label(r2)
            self.lang_lbl.pack(side="left", padx=(12, 2))
            self.lang_box = ttk.Combobox(r2, values=list(LANG_NAMES.values()), width=10, state="readonly")
            self.lang_box.set(LANG_NAMES[self.lang])
            self.lang_box.pack(side="left")
            self.lang_box.bind("<<ComboboxSelected>>", self.on_lang)
            self.status = ttk.Label(self)
            self.status.pack(fill="x", padx=8)
            pw = ttk.PanedWindow(self, orient="horizontal")
            pw.pack(fill="both", expand=True, padx=6, pady=6)
            left = ttk.Frame(pw)
            self.tree = ttk.Treeview(left, show="tree")
            self.tree.pack(fill="both", expand=True)
            self.tree.bind("<<TreeviewOpen>>", self.on_open)
            self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
            right = ttk.Frame(pw)
            pw.add(left, weight=1)
            pw.add(right, weight=2)
            sr = ttk.Frame(right)
            sr.pack(fill="x")
            self.search_lbl = ttk.Label(sr)
            self.search_lbl.pack(side="left")
            self.q = tk.StringVar()
            ttk.Entry(sr, textvariable=self.q).pack(side="left", fill="x", expand=True, padx=4)
            self.q.trace_add("write", lambda *_: self.fill_list())
            self.lst = ttk.Treeview(right, columns=("a", "b"), show="headings", height=10)
            self.lst.pack(fill="both", expand=True, pady=4)
            self.lst.bind("<<TreeviewSelect>>", self.on_row)
            self.txt = tk.Text(right, height=8, wrap="word")
            self.txt.pack(fill="x")
            self.info = ttk.Label(right)
            self.info.pack(fill="x")
            rb = ttk.Frame(right)
            rb.pack(fill="x", pady=4)
            for k, c in (("apply", self.apply), ("revert", self.revert), ("export", self.export_text), ("import", self.import_text)):
                self.add_btn(rb, k, c)
            self.apply_lang()
            self.status.config(text=self.t("none"))

        def apply_lang(self):
            for k, w in self.widgets.items():
                w.config(text=self.t(k))
            self.path_lbl.config(text=self.t("path"))
            self.lang_lbl.config(text=self.t("lang") + ":")
            self.search_lbl.config(text=self.t("search"))
            self.lst.heading("a", text=self.t("id_col"))
            self.lst.heading("b", text=self.t("text_col"))

        def on_lang(self, _=None):
            nm = self.lang_box.get()
            self.lang = next(c for c, n in LANG_NAMES.items() if n == nm)
            self.st["lang"] = self.lang
            save_settings(self.st)
            self.apply_lang()

        def on_close(self):
            if self.s and self.s.has_pending() and not messagebox.askyesno("", self.t("unsaved")):
                return
            self.destroy()

        # ---- buka sumber
        def pick(self, title, types):
            return filedialog.askopenfilename(title=title, filetypes=types)

        def open_iso(self):
            p = self.pick(self.t("open_iso"), [("ISO", "*.iso *.bin *.img"), (self.t("all"), "*.*")])
            if p:
                self.path_var.set(p)
                self.open_path()

        def open_wad(self):
            p = self.pick(self.t("open_wad"), [("WAD", "*.wad *.WAD"), (self.t("all"), "*.*")])
            if p:
                self.path_var.set(p)
                self.open_path()

        def open_folder(self):
            p = filedialog.askdirectory(title=self.t("open_folder"))
            if p:
                self.path_var.set(p)
                self.open_path()

        def open_path(self):
            p = self.path_var.get().strip()
            if not p:
                return
            self.say(self.t("reading"))

            def work():
                try:
                    s = open_source(p)
                    self.after(0, lambda: self.loaded(s, p))
                except Exception as e:
                    txt = traceback.format_exc()
                    log(txt)
                    msg = "%s\n\n%s" % (e, self.t("err_where", log_error(txt)))
                    self.after(0, lambda: messagebox.showerror(self.t("err"), msg))
            threading.Thread(target=work, daemon=True).start()

        def loaded(self, s, p):
            self.s = s
            self.meta.clear()
            self.cur = None
            self.tree.delete(*self.tree.get_children())
            self.clear_editor()
            if s.kind == "iso":
                for pk in s.pak_names():
                    n = self.tree.insert("", "end", text=pk)
                    self.meta[n] = ("pak", pk)
                    self.tree.insert(n, "end", text="...")
            else:
                self.fill_wads("", None)
            self.status.config(text="GoW%s - %s  [%s]" % (s.game, os.path.basename(p), s.method))

        # ---- pohon: PAK -> WAD -> entri teks
        def fill_wads(self, parent, pak):
            for r in self.s.wads_in(pak):
                mark = " *" if r.name in self.s.pending else ""
                it = self.tree.insert(parent, "end", text=r.name + mark)
                self.meta[it] = ("wad", r, pak)
                self.tree.insert(it, "end", text="...")

        def placeholder(self, item):
            ch = self.tree.get_children(item)
            return len(ch) == 1 and self.tree.item(ch[0], "text") == "..."

        def on_open(self, _=None):
            item = self.tree.focus()
            m = self.meta.get(item)
            if not m or not self.placeholder(item):
                return
            self.tree.delete(*self.tree.get_children(item))
            if m[0] == "pak":
                self.fill_wads(item, m[1])
            elif m[0] == "wad":
                for e in self.s.text_entries(m[1], m[2]):
                    if e["kind"] == "msgs":
                        raw, _e = self.s.entry_raw(m[1], m[2], e["name"])
                        label = self.t("msgs_n", e["name"], len(MsgDoc(raw).items))
                    else:
                        label = self.t("flp_n", e["name"])
                    n = self.tree.insert(item, "end", text=label)
                    self.meta[n] = ("entry", m[1], m[2], e["name"], e["kind"])

        def on_tree_select(self, _=None):
            m = self.meta.get(self.tree.focus())
            if m and m[0] == "entry":
                self.load_entry(m[1], m[2], m[3], m[4])

        # ---- editor
        def clear_editor(self):
            self.lst.delete(*self.lst.get_children())
            self.rows = {}
            self.txt.delete("1.0", "end")
            self.info.config(text="")

        def load_entry(self, ref, pak, name, kind):
            raw, e = self.s.entry_raw(ref, pak, name)
            cur = {"ref": ref, "pak": pak, "name": name, "kind": kind, "orig": raw}
            if kind == "msgs":
                cur["doc"] = MsgDoc(raw)
            else:
                cur["strs"] = [[off, bs] for off, bs in flp_strings(raw)]
            self.cur = cur
            self.fill_list()
            self.txt.delete("1.0", "end")
            self.show_budget()

        def fill_list(self):
            if not self.cur:
                return
            self.lst.delete(*self.lst.get_children())
            self.rows = {}
            k = self.q.get().strip().lower()
            if self.cur["kind"] == "msgs":
                for i, it in enumerate(self.cur["doc"].items):
                    if k and k not in it["text"].lower() and k != str(it["id"]):
                        continue
                    star = " *" if it["text"] != it["orig"] else ""
                    row = self.lst.insert("", "end", values=(str(it["id"]), it["text"].replace("\n", " ")[:90] + star))
                    self.rows[row] = i
            else:
                for i, (off, bs) in enumerate(self.cur["strs"]):
                    txt = bs.decode("latin-1")
                    if k and k not in txt.lower():
                        continue
                    row = self.lst.insert("", "end", values=("0x%X" % off, txt[:90]))
                    self.rows[row] = i

        def on_row(self, _=None):
            if not self.cur:
                return
            sel = self.lst.selection() or ((self.lst.focus(),) if self.lst.focus() else ())
            if not sel or sel[0] not in self.rows:
                return
            i = self.rows[sel[0]]
            self.txt.delete("1.0", "end")
            if self.cur["kind"] == "msgs":
                self.txt.insert("1.0", self.cur["doc"].items[i]["text"])
            else:
                bs = self.cur["strs"][i][1]
                self.txt.insert("1.0", bs.decode("latin-1"))
                self.info.config(text=self.t("flp_note", len(bs)))

        def show_budget(self):
            if not self.cur or self.cur["kind"] != "msgs":
                self.info.config(text="")
                return
            try:
                used = len(self.cur["doc"].build())
            except UnicodeEncodeError:
                return
            if self.s.kind == "iso":
                size = next(e for e in parse_wad(self.s.wad_data(self.cur["ref"], self.cur["pak"]))
                            if e["name"] == self.cur["name"])["size"]
                self.info.config(text=self.t("budget", used, (size + 15) & ~15))
            else:
                self.info.config(text=self.t("budget_free", used))

        def selected_index(self):
            sel = self.lst.selection() or ((self.lst.focus(),) if self.lst.focus() else ())
            if sel and sel[0] in self.rows:
                return self.rows[sel[0]]
            return None

        def widget_text(self):
            return self.txt.get("1.0", "end-1c").replace("\r\n", "\n")

        def apply(self):
            if not self.cur:
                messagebox.showinfo("", self.t("pick_entry"))
                return
            i = self.selected_index()
            if i is None:
                return
            c = self.cur
            new_text = self.widget_text()
            try:
                if c["kind"] == "msgs":
                    it = c["doc"].items[i]
                    old = it["text"]
                    it["text"] = new_text
                    try:
                        data = c["doc"].build()
                    except UnicodeEncodeError as e:
                        it["text"] = old
                        messagebox.showerror("", self.t("bad_char", new_text[e.start:e.end]))
                        return
                    note = self.commit(c, data, lambda: it.__setitem__("text", old))
                else:
                    off, bs = c["strs"][i]
                    try:
                        nb = new_text.encode("latin-1")
                    except UnicodeEncodeError as e:
                        messagebox.showerror("", self.t("bad_char", new_text[e.start:e.end]))
                        return
                    if len(nb) != len(bs):
                        messagebox.showerror("", self.t("len_diff", len(bs), len(nb)))
                        return
                    raw = bytearray(self.s.entry_raw(c["ref"], c["pak"], c["name"])[0])
                    raw[off:off + len(nb)] = nb
                    note = self.commit(c, bytes(raw), None)
                    if note is not None:
                        c["strs"][i][1] = nb
                if note is not None:
                    self.mark_pending(c["ref"])
                    self.fill_list()
                    self.show_budget()
                    messagebox.showinfo("OK", self.t("applied", note))
            except Exception as e:
                self.fail(e)

        def commit(self, c, data, undo):
            """Tulis ke sesi (memori). Return catatan, atau None kalau dibatalkan."""
            try:
                return self.s.apply_entry(c["ref"], c["pak"], c["name"], data)
            except TooLong as e:
                if self.s.kind == "iso":
                    if undo:
                        undo()
                    messagebox.showerror("", self.t("too_long", e))
                    return None
                if messagebox.askyesno("", self.t("grow_ask", e)):
                    return self.s.apply_entry(c["ref"], c["pak"], c["name"], data, allow_grow=True)
                if undo:
                    undo()
                return None

        def revert(self):
            i = self.selected_index()
            if self.cur and self.cur["kind"] == "msgs" and i is not None:
                it = self.cur["doc"].items[i]
                self.txt.delete("1.0", "end")
                self.txt.insert("1.0", it["orig"])

        def mark_pending(self, ref):
            for it, m in self.meta.items():
                if m[0] == "wad" and m[1].name == ref.name:
                    txt = self.tree.item(it, "text")
                    if not txt.endswith(" *"):
                        self.tree.item(it, text=txt + " *")

        # ---- ekspor / impor seluruh teks
        def export_text(self):
            if not self.cur or self.cur["kind"] != "msgs":
                messagebox.showinfo("", self.t("pick_entry"))
                return
            p = filedialog.asksaveasfilename(initialfile=self.cur["name"] if self.cur["name"].lower().endswith(".txt") else self.cur["name"] + ".txt")
            if p:
                open(p, "wb").write(self.cur["doc"].export_text())
                messagebox.showinfo("OK", self.t("exported", p))

        def import_text(self):
            if not self.cur or self.cur["kind"] != "msgs":
                messagebox.showinfo("", self.t("pick_entry"))
                return
            p = self.pick(self.t("import"), [(self.t("files"), "*.txt"), (self.t("all"), "*.*")])
            if not p:
                return
            try:
                doc = MsgDoc(open(p, "rb").read())
                if not doc.items:
                    messagebox.showerror("", "Tidak ada pesan '*N*' di file itu.")
                    return
                c = self.cur
                old = c["doc"]
                c["doc"] = doc
                note = self.commit(c, doc.build(), lambda: c.__setitem__("doc", old))
                if note is not None:
                    self.mark_pending(c["ref"])
                    self.fill_list()
                    self.show_budget()
                    messagebox.showinfo("OK", self.t("imported", len(doc.items)) + "\n" + self.t("applied", note))
            except Exception as e:
                self.fail(e)

        # ---- simpan / backup
        def do_save(self):
            if not self.s or not self.s.has_pending():
                messagebox.showinfo("", self.t("no_pending"))
                return
            if not messagebox.askyesno(self.t("save"), self.t("confirm_save", len(self.s.pending))):
                return
            try:
                lines = self.s.save()
                for it, m in self.meta.items():
                    if m[0] == "wad":
                        txt = self.tree.item(it, "text")
                        if txt.endswith(" *"):
                            self.tree.item(it, text=txt[:-2])
                if self.cur:
                    self.load_entry(self.cur["ref"], self.cur["pak"], self.cur["name"], self.cur["kind"])
                messagebox.showinfo("OK", self.t("saved", "\n".join(lines)))
            except Exception as e:
                self.fail(e)

        def do_save_as(self):
            m = self.meta.get(self.tree.focus())
            if not m or m[0] not in ("wad", "entry"):
                messagebox.showinfo("", self.t("pick_wad"))
                return
            ref, pak = m[1], m[2]
            p = filedialog.asksaveasfilename(initialfile=ref.name)
            if p:
                try:
                    self.s.save_as(ref, p, pak)
                    messagebox.showinfo("OK", self.t("saved", p))
                except Exception as e:
                    self.fail(e)

        def do_restore(self):
            if not self.s:
                return
            try:
                n = self.s.restore()
                if self.cur:
                    self.load_entry(self.cur["ref"], self.cur["pak"], self.cur["name"], self.cur["kind"])
                messagebox.showinfo("OK", self.t("restored", n))
            except Exception as e:
                self.fail(e)

        def do_discard(self):
            if self.s:
                self.s.discard()
                for it, m in self.meta.items():
                    if m[0] == "wad" and self.tree.item(it, "text").endswith(" *"):
                        self.tree.item(it, text=self.tree.item(it, "text")[:-2])
                if self.cur:
                    self.load_entry(self.cur["ref"], self.cur["pak"], self.cur["name"], self.cur["kind"])
                messagebox.showinfo("", self.t("discarded"))

        # ---- pindai semua WAD (ISO)
        def scan_all(self):
            if not self.s or self.s.kind != "iso":
                messagebox.showinfo("", self.t("cat_open_first"))
                return

            def work():
                try:
                    n = self.s.scan_all(lambda i, t, nm: self.say(self.t("scanning", i, t, nm)))
                    self.after(0, lambda: self.after_scan(n))
                except Exception as e:
                    self.after(0, lambda: self.fail(e))
            threading.Thread(target=work, daemon=True).start()

        def after_scan(self, n):
            for it, m in list(self.meta.items()):
                if m[0] == "pak" and not self.placeholder(it):
                    self.tree.delete(*self.tree.get_children(it))
                    self.fill_wads(it, m[1])
            self.status.config(text="GoW%s" % self.s.game)
            messagebox.showinfo("OK", self.t("scanned", n))

        # ---- katalog
        def show_catalog(self):
            win = tk.Toplevel(self)
            win.title(self.t("cat_title"))
            win.geometry("640x420")
            lst = ttk.Treeview(win, columns=("g", "w", "e"), show="headings")
            for c, w, h in (("g", 60, "Game"), ("w", 140, "WAD"), ("e", 400, "Entri teks")):
                lst.heading(c, text=h)
                lst.column(c, width=w)
            lst.pack(fill="both", expand=True, padx=6, pady=6)
            for g, d in catalog().items():
                for wad, es in sorted(d.items()):
                    lst.insert("", "end", values=(g, wad, ", ".join("%s (%s)" % (e[0], e[1]) for e in es)))

            def go():
                sel = lst.selection()
                if not sel:
                    return
                wad = lst.item(sel[0], "values")[1]
                if not self.s:
                    messagebox.showinfo("", self.t("cat_open_first"))
                elif not self.goto_wad(wad):
                    messagebox.showinfo("", self.t("not_found"))
            ttk.Button(win, text=self.t("open_this"), command=go).pack(pady=6)

        def goto_wad(self, name):
            ref = self.s.by_name.get(name.upper())
            if not ref:
                return False
            if self.s.kind == "iso":
                for it, m in list(self.meta.items()):
                    if m[0] == "pak" and any(l[0] == m[1] for l in ref.locs):
                        self.tree.item(it, open=True)
                        if self.placeholder(it):
                            self.tree.focus(it)
                            self.on_open()
            for it, m in self.meta.items():
                if m[0] == "wad" and m[1].name == name.upper():
                    self.tree.item(it, open=True)
                    self.tree.focus(it)
                    self.tree.selection_set(it)
                    if self.placeholder(it):
                        self.on_open()
                    return True
            return False

        def dump_log(self):
            out = ["=== LOG ==="] + LOG
            if self.s:
                out.append("=== sumber: %s, GoW%s, %d WAD ===" % (self.s.kind, self.s.game, len(self.s.refs)))
            open("gow_debug.log", "w").write("\n".join(out))
            messagebox.showinfo("Log", self.t("log_done"))

    App().mainloop()


if __name__ == "__main__":
    run_gui()
