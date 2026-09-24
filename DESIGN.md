# BrainFrog TUI — Design Guidelines (v2)

> File panduan visual BrainFrog. Ditulis untuk dibaca manusia ATAU disuntik ke
> Claude Code (mis. lewat `BRAINFROG.md`) sebagai referensi wajib setiap kali
> agent nyentuh kode UI.

## 0. Changelog dari draft v1

- **Wordmark & proporsi splash screen** sekarang diambil dari spesifikasi hasil
  analisis referensi OpenCode (951×985px) — sudah **diverifikasi lewat kode**
  (lihat §2) bahwa ASCII block art-nya align dan benar-benar kebentuk 9 huruf
  `BRAINFROG`, bukan cuma asumsi.
- **Token warna diperbarui** ke nilai dari dokumen referensi (`#0a0a0a` /
  `#1e1e1e` / `#00FF66`) — ini gantiin nilai di v1 yang lebih redup, karena
  dokumen referensi ini hasil pengukuran pixel langsung dari acuan visual.
- **Footer dikembalikan ke pola sudut-kiri/sudut-kanan** (`~` kiri bawah,
  versi kanan bawah) — v1 sempat nyaranin digabung ke baris hint, itu salah,
  dibatalkan. Pola pinned-corner ini standar di TUI (vim/tmux status line)
  dan cocok sama referensi.
- **Ditambahkan:** batasan kontras untuk `text.muted` (lihat §3.1) dan catatan
  ketegangan filosofi "satu aksen" vs kebutuhan warna semantik untuk error
  (lihat §3.2) — dua hal ini butuh keputusan sadar, bukan tebakan.
- Bagian yang TIDAK ada di dokumen referensi (chat/history area, loading
  indicator, error/success banner, menu picker) dipertahankan dari v1 dengan
  token warna yang sudah disesuaikan.

---

## 1. Prinsip Desain

- **Satu aksen fungsional, bukan pelangi.** Hijau (`#00FF66`) adalah SATU-
  SATUNYA warna aksen untuk state aktif/fokus/cursor. Jangan tambah warna lain
  untuk "variasi" — kalau butuh bedain sesuatu, pakai kontras kegelapan atau
  simbol, bukan hue baru.
- **Tenang di splash, padat-informasi begitu ada history.** Splash screen
  boleh lega dan minim; begitu percakapan mulai, prioritas berubah ke
  keterbacaan dan densitas informasi.
- **Degradasi anggun.** Harus tetap kebaca di 16-color terminal, `NO_COLOR=1`,
  dan terminal non-Unicode. Lihat §7.
- **Kontras rendah itu pilihan sadar, bukan default.** Kalau suatu elemen
  didesain low-contrast (footer, placeholder), itu HARUS karena elemen itu
  memang boleh diabaikan mata. Kalau elemen itu bawa informasi penting
  (error, konfirmasi destruktif), dia tidak boleh pakai token low-contrast.

---

## 2. Wordmark & Identitas — Splash Screen

### 2.1 ASCII Block Art (terverifikasi)

```text
█▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █   █▀▀ █▀▀▄ ▄▀ ▀▄ ▄▀▀▀
█▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█   █▀  █▀▀▄ █▄▄▄█ █ ▀█
▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀   ▀   ▀  ▀ ▀   ▀ ▀▀▀▀
```

Sudah dicek programatis: ke-3 baris panjangnya sama persis (46 karakter), dan
kalau di-split di kolom yang kosong di ketiga baris sekaligus, hasilnya
PERSIS 9 grup huruf: `B R A I N` + `F R O G`. Notch mata kodok di huruf `O`
(grup ke-8: `▄▀ ▀▄` di baris atas, `█▄▄▄█` di baris tengah) juga sesuai
maksud desain — ini yang bikin dia kerasa "FROG", bukan cuma kotak generik.

**Catatan legibilitas:** huruf `B` (grup 1) dan `R` (grup 2 & 7) nyaris
identik — bedanya cuma 1 karakter di baris paling bawah (`▀▀▀ ` vs `▀  ▀`).
Ini standar buat block-font sederhana, tapi di font terminal kecil bisa
kurang kebaca bedanya. Bukan blocker, tapi kalau brand ini bakal dipakai di
ukuran kecil (favicon, avatar CLI), pertimbangkan varian yang lebih tegas.

### 2.2 Dua-Nada

- `BRAIN` → `color.text.secondary` (`#808080`)
- `FROG` → `color.text.primary` (`#eeeeee`), kecuali notch mata di huruf `O`
  yang pakai `color.accent.green` (`#00FF66`)

### 2.3 Fallback tanpa Unicode

Kalau terminal nggak support box-drawing characters, turun ke:
```text
[ BRAINFROG ]
```
Teks polos, tetap dua-nada kalau bisa, center-aligned.

---

## 3. Sistem Warna & Token

| Token | Hex | ANSI 256 | 16-color fallback | Peruntukan |
|---|---|---|---|---|
| `color.bg.canvas` | `#0a0a0a` | 232 | black | Background utama |
| `color.surface.panel` | `#1e1e1e` | 234 | black (bright) | Background panel input |
| `color.accent.green` | `#00FF66` | 46 | bright green | Aksen garis kiri panel, cursor, mode aktif, mata `O` |
| `color.text.primary` | `#eeeeee` | 255 | white | Teks ketikan user, wordmark FROG, shortcut key |
| `color.text.secondary` | `#808080` | 244 | gray | Wordmark BRAIN, label shortcut, nama model |
| `color.text.muted` | `#555555` | 240 | gray (dim) | Separator `·`, footer, placeholder — **lihat batasan §3.1** |
| `color.error` | `#E5534B` | 167 | red | **Baru** — isi pesan error. Tidak ada di dokumen referensi karena referensi cuma fokus splash; wajib ada untuk state percakapan (§6) |
| `color.warning` | `#E3B341` | 179 | yellow | **Baru** — konfirmasi destruktif |

### 3.1 Batasan `text.muted`

Dicek kontrasnya: `#555555` di atas `#1e1e1e` (panel) = **2.24:1**, di atas
`#0a0a0a` (canvas) = **2.66:1**. Keduanya di bawah ambang WCAG AA (4.5:1
teks normal / 3:1 UI besar). **Ini boleh dan disengaja** untuk: footer path,
separator dot, placeholder kosong. **Ini TIDAK BOLEH dipakai** untuk: isi
pesan error, konfirmasi yang butuh perhatian user, atau teks apapun yang
kalau kelewat bikin user salah paham state aplikasi.

### 3.2 Ketegangan "satu aksen" vs warna error

Prinsip §1 bilang "satu aksen fungsional". Tapi error/warning state tanpa
warna beda itu bahaya — user bisa nggak sadar ada masalah. Resolusi yang
dipakai di dokumen ini: **splash screen dan idle state tetap monokrom + 1
aksen hijau ketat** (sesuai referensi), tapi **begitu masuk state
percakapan aktif (§6, State 2)**, `color.error` dan `color.warning` boleh
dipakai — SELALU dibarengi simbol (`✗`/`⚠`), bukan warna doang, biar tetap
kebaca di 16-color/`NO_COLOR`.

---

## 4. Layout Splash Screen (State 0)

Proporsi dari analisis referensi (skala ke lebar/tinggi terminal aktif, bukan
piksel absolut):

| Elemen | Posisi Y (dari tinggi terminal) | Lebar |
|---|---|---|
| Wordmark | 38–45% | ~37% lebar canvas, center |
| Gap | 45–49% | — |
| Panel Input | 49–58% | 70% lebar canvas (`int(cols * 0.70)`), center |
| Gap kecil | 58–59% | — |
| Shortcut bar | 59–61% | Rata kiri panel input |
| Footer | 96–97% (baris terakhir) | Pinned sudut kiri & kanan |

```text
┌──────────────────────────────────────────────────────────┐
│                                                            │
│                    (± 38% ruang kosong atas)              │
│                                                            │
│              █▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █   █▀▀ █▀▀▄ ▄▀ ▀▄ ▄▀▀▀ │
│              █▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█   █▀  █▀▀▄ █▄▄▄█ █ ▀█ │
│              ▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀   ▀   ▀  ▀ ▀   ▀ ▀▀▀▀ │
│                                                            │
│        ╭──────────────────────────────────────────╮       │
│        │▌ Tanya BrainFrog… "Jelaskan arsitektur..." │       │ ← 70% lebar
│        │▌ agentic · claude-sonnet-5 · agentic_dev   │       │
│        ╰──────────────────────────────────────────╯       │
│        tab mode   /models pick   /? help   @ file          │
│                                                            │
│                    (ruang bawah fleksibel)                 │
│ ~                                                   v0.1.0 │
└──────────────────────────────────────────────────────────┘
```

### Spesifikasi Panel Input
- Garis aksen kiri: `▌` atau `▎`, warna `color.accent.green`, tinggi menyamai panel.
- Baris 1: placeholder `Tanya BrainFrog… "..."` dalam `color.text.secondary`;
  begitu user ngetik, teks jadi `color.text.primary`.
- Baris 2 (metadata): `mode` (hijau aksen) `·` (muted) `model` (putih) `·` (muted) `repo` (abu-abu).

### Spesifikasi Footer
- Kiri bawah: cwd ringkas (`~` atau `~/tools/agentic_dev`), `color.text.muted`.
- Kanan bawah: versi (`v0.1.0`), `color.text.muted`.
- **Perlu dikonfirmasi:** dokumen referensi pakai shortcut `tab mode` /
  `/models pick` / `/? help`, tapi screenshot awal project ini nunjukkin
  `tab models` / `ctrl+p help`. Pastiin salah satu — kalau keybinding
  beneran udah diubah, update juga bagian ini; kalau belum, jangan asal
  ikutin dokumen referensi.

---

## 5. Tipografi & Ikonografi (berlaku di semua state)

- Box-drawing: `╭╮╰╯` (rounded) untuk panel biasa, `┌┐└┘` (tegas) untuk
  modal/overlay penting — biar user bisa bedain "panel biasa" vs "ini butuh
  perhatian/keputusan".
- Simbol status (dipakai BARENGAN warna, bukan gantiin — lihat §3.2):
  `✓` success · `✗` error · `⚠` warning · `ℹ` info
- Spinner loading: braille frames `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, warna `color.accent.green`.
- Ikon brand: 🐸 boleh dipakai TERBATAS di compact header (§6, State 2+),
  bukan di badan teks berulang-ulang.

---

## 6. Siklus Hidup State

**State 0 — Splash/Empty:** komposisi §4. Muncul saat pertama run atau
setelah `/clear`.

**State 1 — Mengetik:** teks masuk ke panel input warna `text.primary`.
`@` memicu popup autocomplete file tepat di bawah cursor. `/` memicu menu
command (overlay border tegas, lihat §5) tanpa geser layout yang lain.

**State 2 — Streaming/Percakapan Aktif:** splash TIDAK diulang tiap giliran.
Wordmark collapse jadi compact header 1 baris (`🐸 BrainFrog` atau hilang).
Layout jadi top-anchored, history area yang scroll/grow:
- Pesan user: prefix `›`, `text.primary`.
- Pesan assistant: prefix `🐸`, `text.primary`; sub-detail (tool call, diff)
  di-indent 2 spasi, warna `text.secondary`.
- Loading: spinner braille + teks status (`⠋ Membaca file...`).
- Error: box border `color.error`, prefix `✗` — **ini yang butuh warna di
  luar aksen hijau, per §3.2.**
  ```
  ╭─ ✗ Error ───────────────────────╮
  │ Gagal konek ke Claude API.      │
  │ Cek ANTHROPIC_API_KEY di .env   │
  ╰─────────────────────────────────╯
  ```
- Warning (konfirmasi destruktif): box border `color.warning`, prefix `⚠`.

**State 3 — Reset:** `/clear` bersihin buffer, render ulang State 0 dengan
metadata runtime terbaru (model/mode/repo bisa udah beda dari sesi sebelumnya).

---

## 7. Responsivitas & Fallback

| Kondisi | Strategi |
|---|---|
| ≥ 80 kolom, ≥ 24 baris | Komposisi penuh — wordmark 3-baris, panel 70%. |
| 60–79 kolom | Panel jadi 85% lebar (biar metadata gak kepotong), padding samping dikurangi. |
| < 60 kolom | Wordmark → `[ BRAINFROG ]` teks polos. Metadata cuma `mode · model`. |
| 256-color / 16-color | `bg`→black, `panel`→234, `accent`→bright_green, `text`→white/gray. |
| Non-Unicode | Garis aksen kiri → `|`, separator → `.`, huruf balok → `#`/`=`/`-`. |
| `NO_COLOR=1` | Semua warna off; state HARUS tetap kebeda lewat simbol (§5). |
| Resize (SIGWINCH) | Listener hitung ulang margin tengah & lebar panel saat refresh. |

---

## 8. Checklist Penerimaan

**Splash (State 0):**
- [ ] Wordmark 9-huruf align sempurna di semua lebar terminal yang didukung.
- [ ] Panel input tepat 70% lebar kolom, center horizontal.
- [ ] Metadata (mode/model/repo) akurat dari runtime, bukan hardcoded.
- [ ] Footer: cwd kiri-bawah, versi kanan-bawah, tetap `text.muted`.
- [ ] Shortcut bar sesuai keybinding YANG BENERAN AKTIF (cross-check §4 catatan).

**Umum (semua state):**
- [ ] Nggak ada elemen nempel langsung ke tepi layar tanpa margin.
- [ ] Semua panel pakai border, bukan teks polos ke background.
- [ ] `text.muted` cuma dipakai untuk elemen dekoratif, bukan info penting (§3.1).
- [ ] Error/warning selalu simbol + warna, bukan warna doang (§3.2).
- [ ] Diuji di `NO_COLOR=1` dan terminal 16-color — semua state tetap bisa dibedain.
- [ ] Diuji di lebar < 60 kolom — teks nggak wrap jelek/kepotong.
- [ ] `@file`, `/models`, `/help` (atau `/?`), `!shell`, history prompt-toolkit tetap 100% jalan — perubahan ini VISUAL ONLY, bukan behavioral.