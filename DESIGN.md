# Spesifikasi Redesign Tampilan Awal BrainFrog CLI

Dokumen ini mendefinisikan spesifikasi desain visual dan interaksi tampilan awal (splash screen / landing state) untuk BrainFrog CLI, mengadaptasi kesederhanaan, proporsi, dan ketenangan komposisi dari referensi OpenCode (canvas 951 × 985 px) dengan identitas merek khas **BRAINFROG**.

---

## 1. Analisis Proporsi & Komposisi Referensi

Berdasarkan telaah mendalam screenshot referensi (951 × 985 px, setara terminal ~95 kolom × ~41 baris):

| Elemen Visual | Posisi Vertikal (Y) | Posisi Horizontal (X) | Lebar Relatif | Tinggi Relatif | Karakteristik Visual |
|---|---|---|---|---|---|
| **Latar Canvas** | 0 – 100% | 0 – 100% | 100% | 100% | Solid near-black (`#0a0a0a` / `#0c0c0c`), tanpa garis tepi luar |
| **Wordmark** | 38.4% – 44.9% | Terpusat (center) | ~37% canvas | ~66 px (3 baris terminal) | Monospace block font geometris, 2-tone (abu-abu & putih) |
| **Jarak (Gap 1)** | 45.0% – 48.8% | – | – | ~39 px (~2 baris terminal) | Ruang kosong yang lega dan tenang |
| **Input Panel** | 48.9% – 57.5% | Terpusat (center) | ~70.6% canvas | ~86 px (2-3 baris terminal) | Charcoal datar (`#1e1e1e`), garis aksen tipis 4px di sisi kiri |
| **Jarak (Gap 2)** | 57.6% – 58.8% | – | – | ~13 px (1 baris kosong) | Jarak rapat fungsional ke shortcut |
| **Shortcut Bar** | 58.9% – 60.5% | Rata kiri panel input | ~25% canvas | ~16 px (1 baris terminal) | Teks monospace kontras rendah dengan shortcut bold |
| **Footer** | 95.6% – 96.6% | Sudut kiri & kanan bawah | Pinned sudut | ~11 px (1 baris terminal) | `~` di kiri bawah, versi di kanan bawah (kontras sangat rendah) |

---

## 2. Identitas Merek: Wordmark Monospace BRAINFROG

Wordmark menggantikan `opencode` dengan tulisan **BRAINFROG** menggunakan karakter balok terminal monospace yang tajam, presisi, dan mudah dibaca:
1. **Konstruksi Huruf:** Menggunakan karakter balok Unicode (`█`, `▀`, `▄`) dengan tinggi 3 baris.
2. **Kesan Geometris & Proporsional:** Terdiri dari 9 huruf (`BRAIN` dan `FROG`) dengan spasi terukur.
3. **Detail Khas Kodok (Frog Touch):** Huruf `O` pada `FROG` dibentuk dengan notch mata kodok geometris pada balok atas (`▄▀ ▀▄`) dan dagu bawah (`█▄▄▄█`), memberikan sentuhan khas BrainFrog tanpa elemen kartun berlebihan.
4. **Dua Nada (Two-Tone Tone Split):**
   - Bagian **`BRAIN`**: Soft neutral gray (`#808080`).
   - Bagian **`FROG`**: Soft white (`#eeeeee`) dengan aksen subtle hijau BrainFrog (`#00FF66`) pada mata kodok.

```text
█▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █   █▀▀ █▀▀▄ ▄▀ ▀▄ ▄▀▀▀
█▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█   █▀  █▀▀▄ █▄▄▄█ █ ▀█
▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀   ▀   ▀  ▀ ▀   ▀ ▀▀▀▀
```

---

## 3. Sistem Warna & Desain Token

Menghindari gradien mencolok, neon bertumpuk, atau panel ramai. Warna hijau BrainFrog difungsikan secara disiplin sebagai **satu aksen fungsional tunggal**.

| Token Desain | Nilai Hex | Kode ANSI / RGB | Peruntukan |
|---|---|---|---|
| `color.bg.canvas` | `#0a0a0a` | `rgb(10,10,10)` | Latar belakang utama terminal |
| `color.surface.panel` | `#1e1e1e` | `rgb(30,30,30)` | Permukaan panel input datar |
| `color.accent.green` | `#00FF66` | `rgb(0,255,102)` | Garis aksen kiri panel, kursor aktif, mode aktif |
| `color.text.primary` | `#eeeeee` | `rgb(238,238,238)` | Teks ketikan user, wordmark FROG, shortcut key |
| `color.text.secondary`| `#808080` | `rgb(128,128,128)` | Wordmark BRAIN, label shortcut, placeholder |
| `color.text.muted` | `#555555` | `rgb(85,85,85)` | Separator dot (`·`), nama repo/proyek, footer |
| `color.accent.stripe` | `#00FF66` | `rgb(0,255,102)` | Karakter balok aksen kiri panel (`▌`) |

---

## 4. Struktur Layar & Komponen Tampilan Awal

### 4.1 Diagram Tata Letak Terminal

```text
+-----------------------------------------------------------------------------------+
|                                                                                   |
|                                (Ruang Vertikal ~38%)                              |
|                                                                                   |
|                █▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █   █▀▀ █▀▀▄ ▄▀ ▀▄ ▄▀▀▀                     |
|                █▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█   █▀  █▀▀▄ █▄▄▄█ █ ▀█                     |
|                ▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀   ▀   ▀  ▀ ▀   ▀ ▀▀▀▀                     |
|                                                                                   |
|                                (Jarak Lega: 2 baris)                              |
|                                                                                   |
|            +---------------------------------------------------------+            |
|            |▌ Tanya BrainFrog… "Jelaskan arsitektur proyek ini"      |  (~70% W)  |
|            |▌ agentic · gemini-3.8-flash-high  agentic_dev           |            |
|            +---------------------------------------------------------+            |
|                                                                                   |
|            tab mode   /models pick   /? help   @ file                             |
|                                                                                   |
|                                (Ruang Bawah Fleksibel)                            |
|                                                                                   |
| ~                                                                         v0.1.0  |
+-----------------------------------------------------------------------------------+
```

### 4.2 Spesifikasi Komponen

1. **Headerless Canvas:**
   - Tidak ada bar status berderet di atas, border kotak ganda, kartu memori, atau ikon cuaca/baterai.
   - Layar bersih memfokuskan pandangan pengguna ke area input sentral.

2. **Panel Input Datar (Charcoal Flat Panel):**
   - **Lebar:** Tepat 70% dari lebar kolom terminal saat ini (dihitung dinamis: `int(cols * 0.70)`), diposisikan di tengah horizontal.
   - **Sisi Kiri:** Garis aksen tipis vertikal menggunakan karakter setengah balok kiri (`▌` atau `▎`) dengan warna `color.accent.green`.
   - **Baris 1 (Prompt & Placeholder):**
     - Placeholder default: `Tanya BrainFrog… "Apa yang ingin Anda bangun?"`.
     - Kursor atau teks yang sedang diketik user berwarna `color.text.primary`.
   - **Baris 2 (Metadata Dinamis):**
     - `mode`: Nilai dinamis (default `agentic`) dalam warna hijau aksen.
     - Separator: ` · ` dalam warna abu-abu redup.
     - `model`: Nilai dinamis aktif (mis. `gemini-3.8-flash-high` atau `claude-sonnet-5`) dalam putih lembut.
     - `repo`: Nama repositori aktif (mis. `agentic_dev` atau nama folder root) dalam abu-abu.

3. **Shortcut Bar (Petunjuk Bawah Input):**
   - Tepat 1 baris di bawah panel input, sejajar dengan batas kiri panel.
   - Menampilkan shortcut yang benar-benar aktif di BrainFrog:
     - `tab` / `/provider` : berpindah mode/provider
     - `/models` : memilih model AI secara interaktif
     - `/?` atau `/help` : membuka daftar perintah
     - `@` : melampirkan konteks file

4. **Footer Minimalis:**
   - Berada di baris terbawah terminal.
   - **Kiri Bawah:** Direktori kerja saat ini (diformat ringkas seperti `~` atau `~/tools/agentic_dev`).
   - **Kanan Bawah:** Nomor versi BrainFrog saat ini (`v0.1.0`).
   - Kontras warna sangat rendah (`#555555`) agar tidak mencuri perhatian.

---

## 5. Siklus Hidup State & Perilaku Transisi

1. **State 0: Initial / Empty Splash State (Halaman Awal):**
   - Ditampilkan saat BrainFrog pertama kali dijalankan atau ketika user mengetik `/clear`.
   - Menampilkan komposisi hero terpusat (Wordmark, Panel 70%, Shortcut, Footer).

2. **State 1: Active Input Typing (Mengetik Prompt):**
   - Teks yang diketik user muncul di dalam panel input.
   - Saat user mengetik `@`, popup autocomplete file muncul tepat di bawah kursor.
   - Saat user mengetik `/`, menu perintah slash muncul rapi tanpa merusak tata letak.

3. **State 2: Prompt Submitted / Streaming Conversation (Eksekusi Agen):**
   - Begitu Enter ditekan, CLI bertransisi mulus ke mode percakapan:
     - Hero splash screen tidak diulang-ulang pada tiap giliran.
     - Tampilan beralih ke stream percakapan: query user, System 1 reasoning, eksekusi tool, diff code, dan respons jawaban.
     - Tampilan informasi progres dibuat bersih, berkotak tipis, dan berjarak lega.

4. **State 3: Reset / Return to Splash:**
   - Menjalankan `/clear` membersihkan buffer terminal dan merender ulang State 0 dengan data metadata terbaru.

---

## 6. Responsivitas Terminal & Penanganan Fallback

| Kondisi Terminal | Strategi Penyesuaian |
|---|---|
| **Lebar Standar (>= 80 kolom, >= 24 baris)** | Komposisi penuh: Wordmark balok 3-baris, panel input selebar 70%, metadata lengkap, footer sudut. |
| **Lebar Sedang (60 – 79 kolom)** | Panel input diperlebar menjadi 85% kolom agar metadata tidak terpotong. Padding samping dikurangi secara proporsional. |
| **Lebar Sempit (< 60 kolom)** | Wordmark balok disederhanakan menjadi teks monospace ringkas `[ BRAINFROG ]`. Metadata disusun ringkas `mode · model`. |
| **Terminal Tanpa TrueColor (256-color / 16-color)** | Warna dipetakan ke palette ANSI standar: background `black`, panel `234`, aksen `bright_green`, teks `white` / `grey`. |
| **Terminal Tanpa Unicode (ASCII Fallback)** | Garis aksen kiri menggunakan `|`, pemisah menggunakan `.`, dan balok huruf menggunakan karakter ASCII murni (`#`, `=`, `-`). |
| **Event Terminal Resize (SIGWINCH)** | Listener resize mendeteksi perubahan `columns` dan `lines`, menghitung ulang margin tengah secara otomatis saat screen refresh. |

---

## 7. Kriteria Penerimaan (Acceptance Criteria)

- [ ] **Wordmark BRAINFROG:** Tampil tajam menggunakan balok monospace dengan detail mata kodok geometris pada huruf `O`, tanpa clipart/emoji berantakan.
- [ ] **Warna & Palet:** Latar belakang terminal konsisten near-black (`#0a0a0a`), panel berlatar charcoal (`#1e1e1e`), dan aksen hijau `#00FF66` hanya pada aksen garis & status aktif.
- [ ] **Proporsi Input:** Panel input berada di tengah secara horizontal dan selebar ~70% kolom terminal.
- [ ] **Metadata Dinamis:** Mode (`agentic`), model aktif (`gemini-3.8-flash-high`/`claude-sonnet-5`), dan folder proyek ditampilkan secara akurat dari state runtime aplikasi.
- [ ] **Shortcut & Footer:** Shortcut fungsional berada tepat di bawah panel; footer menampilkan path `~` di kiri dan versi di kanan pada baris terbawah.
- [ ] **Kompatibilitas Fitur:** Autocomplete `@file`, perintah `/models`, `/help`, `! shell`, dan history prompt-toolkit tetap berfungsi 100% normal tanpa regresi.
