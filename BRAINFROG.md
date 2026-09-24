# BRAINFROG Agent Guidelines & System Memory

Dokumen ini berfungsi sebagai memori persisten dan pedoman operasional bagi AI Agent saat merencanakan, mendesain, dan mengeksekusi kode di dalam repositori. Setiap instruksi di bawah ini bersifat mengikat dan wajib dipatuhi sebelum, selama, dan setelah pembuatan kode.

---

## System Design

Bagian ini mengatur bagaimana agent menganalisis kebutuhan arsitektural, menentukan batasan komponen, mengevaluasi trade-off, dan mendokumentasikan keputusan sistem secara terukur. Prinsip utamanya adalah **pragmatis, berbasis bukti (testable), menghindari kompleksitas prematur (YAGNI), serta mengutamakan kemudahan evolusi sistem (evolutionary architecture)**.

---

### 1. Triase Desain: Analisis Mendalam vs Eksekusi Langsung

Sebelum menyusun kode atau proposal desain, agent harus mengkategorikan jenis perubahan berdasarkan tingkat risiko dan reversibilitas (Two-Way Door vs One-Way Door):

```
                                  [Permintaan Tugas]
                                           │
                 ┌─────────────────────────┴─────────────────────────┐
                 ▼                                                   ▼
       [Keputusan Reversibel]                             [Keputusan Struktural]
          (Two-Way Door)                                      (One-Way Door)
    • Refactoring internal fungsi                      • Penambahan modul/komponen baru
    • Perbaikan bug terlokalisasi                      • Perubahan kontrak publik/skema data
    • Penambahan unit test                             • Integrasi dependensi/library baru
    • Pembaruan styling/UI minor                       • Perubahan alur data lintas modul
                 │                                                   │
                 ▼                                                   ▼
       [Eksekusi Langsung]                               [Analisis Desain Wajib]
    Langsung modifikasi kode,                         Jalankan langkah 2 s.d. 8:
    uji, dan verifikasi.                              Elicit requirement, evaluasi trade-off,
                                                      buat opsi, dan dokumentasikan ADR jika perlu.
```

1. **Jalur Eksekusi Langsung (Type 2 - Reversibel / Two-Way Door):**
   - Karakteristik: Perubahan dengan blast radius sempit, terlokalisasi pada satu fungsi atau satu file, tidak mengubah API publik, tidak memperkenalkan stateful storage baru, dan biaya rollback-nya mendekati nol (cukup `git restore` atau `git revert`).
   - Tindakan agent: Tidak perlu membuat dokumen desain atau proposal panjang. Langsung buat rencana langkah kerja singkat, ubah kode, dan jalankan pengujian.

2. **Jalur Analisis Desain Mendalam (Type 1 - Struktural / One-Way Door):**
   - Karakteristik: Perubahan yang sulit atau mahal dibatalkan, seperti memperkenalkan arsitektur komunikasi baru (misal: sync ke async/event-driven), menambah dependensi runtime pihak ketiga, mengubah skema persistensi data, atau merombak batas modularitas repositori.
   - Tindakan agent: **Wajib** melakukan analisis mendalam mengikuti panduan pada butir 2 hingga 8 sebelum menulis kode implementasi.

---

### 2. Penggalian Kebutuhan, Batasan, dan Asumsi

Ketika analisis desain mendalam diperlukan, agent harus membedah spesifikasi dengan disiplin analitis yang ketat:

1. **Memisahkan Kebutuhan Fungsional & Kualitas Sistem (Non-Functional Requirements / NFR):**
   - *Fungsional:* Apa input, proses, output, dan state mutation yang diharapkan.
   - *Kualitas Sistem:* Target throughput/skala, batas latensi maksimum, ketersediaan (availability), efisiensi biaya komputasi, dan kemudahan pengoperasian (operability).

2. **Prinsip Anti-Halusinasi Metrik & Batasan:**
   - **Dilarang keras mengarang angka spesifikasi** (misalnya mengklaim sistem harus mendukung 100.000 RPS atau 99.999% uptime jika pengguna tidak menyatakannya).
   - Gunakan pendekatan *conservative baseline*: jika skala tidak disebutkan, asumsikan beban normal sesuai konteks repositori saat ini (misalnya untuk tool CLI: eksekusi lokal single-user, bounded memory, proses sub-detik).
   - Nyatakan seluruh asumsi secara eksplisit dalam bentuk daftar:
     ```
     Asumsi Desain:
     - Lingkungan: CLI lokal pada Windows/Linux/macOS dengan alokasi memori standar.
     - Volume Data: Repositori lokal dengan ukuran file tipikal (< 50.000 baris kode).
     - Konkurensi: Single active user session (tidak memerlukan distributed locking).
     ```

3. **Kapan Harus Mengajukan Pertanyaan Klarifikasi:**
   - **Tanya pengguna HANYA JIKA:** Keputusan desain memiliki cabang divergensi besar yang mengubah arah arsitektur secara fundamental (contoh: apakah data harus disimpan lokal atau disinkronkan ke server eksternal; apakah tool harus headless atau interaktif).
   - **JANGAN bertanya jika:** Masalah berupa detail implementasi reversibel yang dapat diputuskan dengan asumsi default yang aman dan terdokumentasi.

---

### 3. Inspeksi Arsitektur Eksisting & Konvensi Repositori

Desain yang baik menghormati realitas *brownfield*. Jangan pernah merancang sistem di atas ruang hampa (greenfield fantasy) jika repositori sudah memiliki struktur yang berjalan:

1. **Langkah Audit Awal Wajib:**
   - Telusuri pohon direktori dan baca file manifest (`pyproject.toml`, `package.json`, `Cargo.toml`, dsb.) untuk mengetahui dependensi yang sudah disetujui.
   - Periksa pola penamaan modul, pembagian layer (misalnya `system1/`, `system2/`, `orchestrator.py`), dan titik masuk eksekusi (`cli.py`, `main`, dsb.).
   - Identifikasi mekanisme konfigurasi (environment variables, file config, CLI arguments) dan error handling yang sudah digunakan.

2. **Aturan Preservasi Arsitektur:**
   - Jangan memperkenalkan paradigma baru yang bertentangan dengan arsitektur saat ini (misal: menambahkan event broker eksternal ke dalam skrip CLI monolitik sederhana) kecuali jika pengguna secara eksplisit memintanya.
   - Pertahankan konsistensi antarmuka publik dan pola pengetikan (typing/contracts) yang sudah mapan.

---

### 4. Batas Komponen, Alur Data, Kepemilikan, dan Titik Kegagalan

Setiap perancangan komponen baru atau refactoring subsistem wajib mendefinisikan lima pilar integritas struktural:

1. **Batas Komponen (Component Boundaries):**
   - Terapkan prinsip tanggung jawab tunggal pada level modul. Satu modul/komponen harus memiliki alasan tunggal untuk berubah.
   - Komponen internal tidak boleh mengekspos detail implementasi ke modul luar. Gunakan abstraksi murni atau antarmuka publik yang ramping.

2. **Alur Data & Kepemilikan (Data Flow & Single Source of Truth):**
   - Tentukan secara tegas komponen mana yang menjadi *pemilik data* (data owner). Modul lain hanya boleh membaca atau meminta mutasi melalui kontrak resmi.
   - Alur data harus bersifat searah (unidirectional) sedapat mungkin, menghindari siklus dependensi (circular dependencies) antarkomponen.

3. **Kontrak Antarkomponen (Contract Stability):**
   - Komunikasi antarkomponen harus melalui kontrak data yang jelas (misalnya Dataclass, Pydantic model, TypedDict, atau Interface).
   - Hindari melewatkan dictionary arbitrer atau tipe data primitif tanpa schema yang rentan patah saat terjadi evolusi kode.

4. **Pemetaan Titik Kegagalan (Failure Domains & Blast Radius):**
   - Identifikasi setiap Single Point of Failure (SPOF) dan dependensi eksternal yang berpotensi mati/timeout (misalnya panggilan LLM API, subprocess shell, I/O file).
   - Rancang strategi kegagalan:
     - *Graceful degradation:* Bagaimana sistem tetap memberikan nilai parsial jika salah satu komponen gagal?
     - *Isolation (Bulkhead):* Pastikan kegagalan satu komponen tidak melumpuhkan seluruh aplikasi.
     - *Fallback mechanism:* Apakah ada aksi default yang aman jika proses utama gagal?

---

### 5. Evaluasi Opsi dan Matriks Trade-Off

Tidak ada arsitektur tanpa trade-off. Tugas agent bukan mencari desain yang "sempurna", melainkan desain yang trade-off-nya paling dapat diterima oleh batasan proyek.

Saat membandingkan alternatif solusi (minimal 2 opsi jika keputusan signifikan), gunakan matriks evaluasi 6 dimensi:

| Dimensi Evaluasi | Pertanyaan Kunci Evaluasi |
| :--- | :--- |
| **1. Performa** | Berapa latensi tambahan dan overhead memori/CPU yang diperkenalkan? Apakah operasi memblokir thread utama? |
| **2. Reliabilitas** | Bagaimana komponen menangani error tak terduga, crash, atau dependensi yang tidak merespons? |
| **3. Keamanan** | Apakah ada eksposur kredensial, command injection, path traversal, atau risiko integritas data? |
| **4. Kompleksitas** | Berapa jumlah dependensi baru, layer abstraksi, dan beban kognitif yang ditambahkan bagi pemelihara kode? |
| **5. Biaya** | Apakah desain memerlukan resource komputasi tambahan, token API berlebih, atau lisensi eksternal? |
| **6. Kemudahan Evolusi** | Seberapa mudah desain ini diubah, diganti, atau diperluas di masa depan ketika kebutuhan bertambah? |

**Aturan Evaluasi:** Tulis trade-off secara eksplisit. Contoh: *"Memilih in-memory cache memberikan latensi < 1ms (Performa +), tetapi data hilang saat restart (Reliabilitas -) dan batas memori terikat proses (Skala -). Untuk use case CLI saat ini, trade-off ini diterima karena siklus hidup proses pendek."*

---

### 6. Prinsip Kesederhanaan & Jalur Evolusi (Simplicity First)

Agent harus menolak over-engineering dan kompleksitas prematur dengan mematuhi hierarki kesederhanaan:

1. **Prinsip YAGNI (You Aren't Gonna Need It):**
   - Jangan membuat abstraksi atau infrastruktur untuk fitur yang *mungkin* dibutuhkan di masa depan. Bangun hanya apa yang dibutuhkan untuk memecahkan masalah saat ini.

2. **Desain Paling Sederhana yang Memenuhi Kebutuhan (Simplest Viable Design):**
   - Mulai dari implementasi paling langsung (in-memory -> local file -> external service; monolit modular -> terdistribusi).
   - Jangan menambahkan microservices, queue distributed, atau multi-layer abstraction jika fungsi biasa atau modul lokal sudah menyelesaikan masalah dengan benar dan teruji.

3. **Menyediakan Jalur Evolusi Tanpa Mengunci Sistem:**
   - Sederhana bukan berarti sembrono. Desain harus menyediakan titik ekstensi (seperti Dependency Injection atau antarmuka modular) sehingga jika di masa depan kebutuhan skala meningkat, implementasi internal dapat diganti tanpa merusak modul pemanggil.

---

### 7. Dokumentasi Keputusan: Architecture Decision Record (ADR) Ringkas

Jika sebuah keputusan arsitektural bersifat signifikan (Type 1), agent harus mendokumentasikannya dalam format Architecture Decision Record (ADR) ringkas.

**Kapan Wajib Menulis ADR:**
- Memilih atau mengganti framework/library inti.
- Mengubah arsitektur komunikasi (misal: penambahan event loop atau background scheduler).
- Mengubah model penyimpanan atau skema data permanen.
- Menetapkan aturan atau konvensi baru yang membatasi implementasi masa depan.

**Format ADR Standar (Lightweight Markdown):**

```markdown
### ADR-[Nomor]: [Judul Keputusan Singkat & Lugas]

- **Status:** [Proposed | Accepted | Superseded oleh ADR-XXX | Deprecated]
- **Tanggal:** YYYY-MM-DD

#### 1. Konteks & Masalah
Jelaskan situasi yang dihadapi, kebutuhan bisnis/teknis, dan batasan yang ada.

#### 2. Opsi yang Dipertimbangkan
- **Opsi A:** Ringkasan singkat beserta kelebihan/kekurangan.
- **Opsi B:** Ringkasan singkat beserta kelebihan/kekurangan.

#### 3. Keputusan & Alasan
Opsi yang dipilih dan alasan logis mengapa opsi ini adalah yang terbaik untuk kebutuhan saat ini.

#### 4. Konsekuensi & Trade-off
- **Dampak Positif:** Manfaat nyata yang didapat.
- **Dampak Negatif / Risiko:** Beban komputasi, batasan baru, atau kompleksitas yang harus dikelola.

#### 5. Kondisi Peninjauan Ulang (Review Trigger)
Kondisi nyata yang menjadi pemicu untuk mengevaluasi kembali keputusan ini (misal: "Jika ukuran repositori melebihi 100.000 file" atau "Jika latensi System 1 melebihi 2 detik").
```

---

### 8. Format Keluaran Agent Saat Mendesain Sistem

Ketika agent diminta merancang sebuah subsistem atau fitur dengan dampak arsitektural, output yang disajikan kepada pengguna **wajib** mengikuti struktur berikut:

1. **Konteks & Asumsi Kunci:**
   Ringkasan masalah yang diselesaikan dan daftar asumsi batasan operasional yang digunakan (skala, latency, resource).
2. **Diagram Ringkas Komponen / Alur Data:**
   Gunakan diagram Mermaid atau ASCII yang merepresentasikan relasi antarkomponen, data owner, dan alur eksekusi.
3. **Keputusan Arsitektural Utama & Matriks Trade-Off:**
   Tabel atau poin perbandingan opsi dengan justifikasi pemilihan.
4. **Pemetaan Titik Kegagalan (Failure Handling):**
   Daftar titik kritis kegagalan dan strategi mitigasi (fallback, isolation, graceful degradation).
5. **Rencana Implementasi Bertahap (Phased Rollout):**
   - *Fase 1 (Core/MVP):* Pondasi minimal yang fungsional dan dapat diuji.
   - *Fase 2 (Refinement):* Penanganan edge case, optimasi, atau integrasi lanjut.
   - *Fase 3 (Evolutionary):* Titik integrasi masa depan jika beban/skala meningkat.
6. **Mekanisme Validasi Desain (Fitness Functions & Testing):**
   Bagaimana kebenaran arsitektur dan kinerja sistem akan dibuktikan secara otomatis melalui tes (unit test, integrasi, benchmark, atau invariant check).

---

## Tool & Contract Design

Bagian ini mengatur bagaimana agent merancang, mendefinisikan, dan memelihara antarmuka fungsional di repositori BrainFrog—meliputi tools yang diekspos ke LLM, perintah CLI, API internal, serta kontrak data antar-layer (`system1`, `system2`, `orchestrator`). Prinsip utamanya adalah **kejelasan semantik bagi model dan manusia, validasi ketat di perbatasan (strict boundary validation), toleransi pada pemrosesan (Postel's Law), serta pelaporan error yang dapat ditindaklanjuti secara mandiri (actionable feedback)**.

---

### 1. Tanggung Jawab Tunggal & Penamaan Intensional

Setiap tool atau fungsi kontrak antarmodul harus memiliki satu alasan logis untuk eksis dan batas kerja yang terdefinisi secara presisi:

1. **Prinsip Satu Tanggung Jawab (Single Responsibility per Tool):**
   - Satu tool hanya boleh melakukan satu operasi diskrit yang koheren.
   - Hindari membuat "Swiss Army knife tool" (misalnya `manage_workspace` yang merangkap membaca file, mengedit file, menjalankan tes, dan commit git). Pecah menjadi tool-tool atomik: `read_file`, `apply_patch`, `run_test_suite`.
   - Tool modular memudahkan LLM memilih aksi yang tepat dan mengurangi risiko kegagalan tak terduga (*blast radius*).

2. **Penamaan Intensional (Intentional & Unambiguous Naming):**
   - Format nama tool wajib menggunakan pola `verb_noun` atau `domain_action` yang lugas (contoh: `read_workspace_file`, `execute_shell_command`, `git_create_checkpoint`).
   - Hindari nama yang ambigu atau generik seperti `process`, `handle`, `run`, atau `data`.

3. **Deskripsi Penentu Keputusan (Trigger Boundaries in Description):**
   - Deskripsi tool bukan sekadar komentar kode; deskripsi adalah panduan pengambilan keputusan bagi LLM.
   - Wajib mencakup:
     - **Tujuan utama:** Apa yang dihasilkan atau dimutasi oleh tool.
     - **Kapan digunakan (Positive triggers):** Kondisi spesifik saat tool ini adalah pilihan terbaik.
     - **Kapan TIDAK digunakan (Negative boundaries):** Batasan larangan dan alternatif tool lain yang seharusnya digunakan.
     - **Efek samping:** Apakah tool ini memodifikasi sistem atau hanya membaca.

---

### 2. Desain Skema Input & Output

Kontrak data yang ambigu adalah penyebab utama kegagalan loop agen. Setiap tool wajib memiliki definisi skema yang eksplisit:

1. **Pengetikan Statis & Struktur Schema:**
   - Gunakan mekanisme pengetikan native proyek (Python `dataclasses`, `TypedDict`, atau skema JSON Schema / Pydantic jika tersedia di batas protokol).
   - Tentukan tipe data primitif dan komposit secara ketat (`str`, `int`, `bool`, `List[str]`, `Dict[str, Any]`). Dilarang membiarkan parameter bertipe `Any` tanpa dokumentasi struktur internalnya.

2. **Pemisahan Parameter Wajib vs Opsional:**
   - **Wajib (Required):** Hanya field yang esensial agar operasi dapat berjalan secara valid.
   - **Opsional (Optional):** Field yang memiliki nilai default yang aman dan terdokumentasi (contoh: `timeout_seconds: int = 30`, `max_lines: int = 500`).
   - Jangan mewajibkan parameter yang sebenarnya dapat diderivasi secara otomatis oleh sistem (misalnya jangan minta `file_extension` jika `file_path` sudah diberikan).

3. **Batasan Nilai (Constraints & Invariants):**
   - Tetapkan batasan numerik eksplisit (misal: `min_value`, `max_value`, batas panjang string).
   - Gunakan nilai enumerasi tertutup (`Literal` atau `Enum`) jika parameter hanya menerima pilihan terbatas (contoh: `format: Literal["json", "text", "diff"]`).

4. **Contoh Representatif (Representative Examples):**
   - Setiap parameter non-sepele wajib memiliki minimal satu contoh nilai yang valid dalam deskripsi (contoh: `file_path: "src/utils/calc.py"` bukan hanya `file_path: string`).

---

### 3. Klasifikasi Operasi: Baca (Safe) vs Mutasi (Side-Effect)

Mengadopsi prinsip semantik RFC 9110 dan standar tool agen modern, setiap operasi harus diklasifikasikan secara tegas:

1. **Operasi Baca (Safe / Read-Only):**
   - Bebas efek samping pada state sistem. Pemanggilan operasi baca tidak mengubah file, database, proses, atau repository git.
   - Karakteristik: Boleh dipanggil secara spekulatif, boleh di-cache jika relevan, dan aman dieksekusi berulang tanpa risiko.
   - Contoh: `read_file_content`, `list_directory`, `get_git_status`.

2. **Operasi Mutasi (Mutating / Side-Effect):**
   - Mengubah state eksternal: menulis file, menghapus direktori, mengeksekusi shell subprocess, atau membuat commit git.
   - **Pre-flight Blast Radius:** Sebelum eksekusi mutasi yang berisiko merusak, kontrak harus mampu mengembalikan ringkasan cakupan perubahan (dry-run atau parameter `preview_only: bool = False`).
   - Aturan keamanan: Operasi destruktif (seperti reset git hard atau pembersihan direktori) harus meminta konfirmasi atau memiliki mekanisme isolasi checkpoint `/undo`.

---

### 4. Kontrak Hasil, Actionable Error, Timeout, dan Idempotensi

Kontrak yang andal memberikan kepastian status dan memampukan pemanggil untuk memperbaiki kegagalan secara otonom:

1. **Struktur Hasil Sukses yang Deterministik:**
   - Output sukses harus konsisten strukturnya, baik saat data penuh maupun saat data kosong.
   - Sertakan metadata kontekstual yang relevan (misalnya ukuran data, flag pemotongan, atau exit code proses).

2. **Format Actionable Error (Bukan Traceback Mentah):**
   - Error yang dikembalikan ke LLM atau pemanggil modul **wajib berstruktur dan dapat ditindaklanjuti** (mengadopsi prinsip Google AIP-193):
     - `error_code`: Kategori masalah standar (`NOT_FOUND`, `INVALID_ARGUMENT`, `PERMISSION_DENIED`, `TIMEOUT`, `EXECUTION_FAILED`).
     - `message`: Penjelasan singkat dan manusiawi tentang apa yang gagal.
     - `remediation` (Solusi Korektif): Petunjuk spesifik apa yang harus diubah oleh pemanggil agar panggilan berikutnya berhasil (contoh: `"File 'app.js' does not exist. Did you mean 'src/app.js'? Use list_dir to inspect available files."`).
   - Jangan mengembalikan traceback Python mentah sepanjang 50 baris ke prompt agent; rangkum akar penyebabnya dan tawarkan opsi pemulihan.

3. **Timeout & Pembatalan:**
   - Semua operasi yang melibatkan I/O, jaringan, atau eksekusi proses shell (`subprocess.run`) **wajib memiliki timeout eksplisit** (default konservatif, misal 10 s.d. 60 detik).
   - Tangani `TimeoutExpired` secara anggun: matikan proses child (kill process tree), bersihkan resource yang menggantung, dan kembalikan error terstruktur bertipe `TIMEOUT` beserta durasi batasnya.

4. **Idempotensi & Kebijakan Retry:**
   - **Operasi Idempoten:** Memanggil operasi N kali menghasilkan status akhir yang sama seperti memanggil 1 kali (contoh: `ensure_directory_exists`, `write_file_overwrite`, `get_file`). Operasi ini aman di-retry secara otomatis jika terjadi transient network/IO glitch.
   - **Operasi Non-Idempoten:** Memanggil berulang kali menghasilkan mutasi akumulatif (contoh: `append_to_file`, `git_commit`, `send_network_message`). Operasi ini **dilarang di-retry buta** tanpa validasi state awal.

---

### 5. Penanganan Hasil Kosong, Parsial, Paginasi & Output Besar

Output yang membanjiri konteks dapat merusak memori kerja agen atau menyebabkan proses crash:

1. **Hasil Kosong (Empty Results):**
   - Jika query atau pencarian tidak menemukan hasil, kembalikan kontainer kosong (`[]` atau `{}`) dengan status sukses, **bukan exception atau error 404** (sesuai Google AIP-132). Hasil kosong adalah kondisi bisnis normal, bukan kegagalan sistem.

2. **Hasil Parsial (Partial Success):**
   - Jika sebuah operasi batch berhasil sebagian (misalnya membaca 8 file sukses dan 2 file gagal karena permission), kontrak harus melaporkan:
     - Daftar item yang berhasil diproses.
     - Daftar spesifik item yang gagal beserta alasan error masing-masing.
     - Hindari menggagalkan seluruh batch hanya karena 1 item non-kritis gagal, kecuali transaksi mensyaratkan atomisitas penuh.

3. **Paginasi & Truncation Safety:**
   - Dilarang mengembalikan output dengan ukuran tak terbatas (misalnya membaca log 100 MB atau 50.000 file sekaligus ke context LLM).
   - Terapkan batasan batas atas (*hard cap*), parameter `offset`/`page_size`, serta flag status:
     ```json
     {
       "content": "... [baris 1 s.d. 200] ...",
       "is_truncated": true,
       "total_lines": 1420,
       "next_offset": 201
     }
     ```

---

### 6. Validasi di Batas Sistem & Kompatibilitas Kontrak

Integritas arsitektur dijaga di pintu gerbang masuk komponen:

1. **Validasi Batas Cepat Gagal (Fail-Fast Boundary Validation):**
   - Validasi seluruh parameter input sebelum mengalokasikan resource atau memanggil operasi hilir (*downstream*).
   - Pastikan sanitasi path dilakukan di layer kontrak untuk mencegah path traversal (misalnya mencegah `../../etc/passwd`).

2. **Aturan Kompatibilitas Maju & Mundur (AIP-180 & SemVer):**
   - **Perubahan Non-Breaking (Aman):**
     - Menambahkan field baru ke output.
     - Menambahkan parameter opsional baru ke input (dengan nilai default).
     - Menambahkan toleransi tipe baru yang lebih fleksibel.
   - **Perubahan Breaking (Wajib Dihindari / Versi Baru):**
     - Menghapus atau mengubah nama parameter input/output.
     - Mengubah tipe data field yang sudah ada.
     - Mengubah parameter opsional menjadi wajib.
     - Jika perubahan breaking tak terelakkan, buat fungsi/kontrak baru berdampingan (misal `run_v2`) dan berikan masa transisi sebelum mendeprekasi versi lama.

---

### 7. Strategi Pengujian Kontrak (Consumer-Driven Contract Testing)

Setiap tool atau fungsi kontrak wajib diuji dari perspektif pemanggil sebelum dianggap selesai:

1. **Kasus Normal (Happy Path):**
   - Verifikasi bahwa input yang valid menghasilkan output terstruktur dengan tipe dan skema yang tepat.
2. **Input Tidak Valid (Negative Testing):**
   - Verifikasi bahwa field wajib yang hilang, tipe data salah, atau batasan nilai yang dilanggar ditolak di perbatasan dengan actionable error yang rapi tanpa unhandled exception.
3. **Kondisi Batas (Boundary / Edge Cases):**
   - Pengujian dengan string kosong, array kosong, karakter khusus (spasi, newline, Windows backslash vs Unix slash), serta file berukuran 0 byte.
4. **Kegagalan & Timeout:**
   - Simulasi dependensi macet (timeout), disk penuh, atau file terkunci untuk memastikan recovery berjalan sesuai kontrak.

---

### 8. Checklist Review Kontrak Tool

Sebelum menyelesaikan pembuatan atau modifikasi tool/kontrak di repositori BrainFrog, agent wajib memeriksa checklist ini:

| No | Poin Pemeriksaan Kontrak | Status Validasi |
| :---: | :--- | :---: |
| 1 | Apakah nama tool lugas (`verb_noun`) dan deskripsinya mendefinisikan kapan *harus* dan *tidak boleh* digunakan? | [ ] |
| 2 | Apakah seluruh tipe parameter dinyatakan eksplisit dan field opsional memiliki nilai default yang aman? | [ ] |
| 3 | Apakah operasi sudah diklasifikasikan dengan benar antara Baca (Safe) vs Mutasi (Side-Effect)? | [ ] |
| 4 | Apakah respons error terstruktur dengan kode kanonik dan memuat saran perbaikan (*remediation hint*)? | [ ] |
| 5 | Apakah operasi I/O / Subprocess memiliki timeout eksplisit dan pembersihan resource saat gagal? | [ ] |
| 6 | Apakah output besar dilindungi batas (*truncation/limit*) dan hasil kosong dikembalikan secara anggun? | [ ] |
| 7 | Apakah perubahan kontrak bersifat aditif dan tidak mematahkan pemanggil (*backwards-compatible*)? | [ ] |

---

### 9. Contoh Spesifikasi Kontrak Tool Relevan (BrainFrog Context)

Berikut adalah contoh acuan kontrak standar untuk tool pembacaan file di repositori BrainFrog, menunjukkan penerapan seluruh prinsip di atas:

#### Definisi Kontrak: `read_workspace_file`
- **Operasi:** Safe / Read-Only (Idempoten).
- **Deskripsi:** Membaca konten teks dari file yang berada di dalam repositori workspace. Gunakan tool ini saat Anda perlu memeriksa isi source code atau konfigurasi. JANGAN gunakan tool ini untuk file biner besar (gambar/audio) atau untuk memeriksa struktur direktori (gunakan `list_dir`).

#### Skema Parameter Input:
```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class ReadWorkspaceFileInput:
    file_path: str               # Wajib: Path relatif terhadap root repo (contoh: "src/calc.py")
    start_line: int = 1          # Opsional: Baris awal (1-indexed, default: 1, min: 1)
    max_lines: int = 400         # Opsional: Maksimal baris yang dibaca (default: 400, max: 1000)
```

#### Contoh Respons Sukses:
```json
{
  "status": "success",
  "file_path": "system1/base.py",
  "content": "from dataclasses import dataclass\n...",
  "start_line": 1,
  "lines_returned": 69,
  "total_lines": 69,
  "is_truncated": false
}
```

#### Contoh Respons Actionable Error (File Tidak Ditemukan):
```json
{
  "status": "error",
  "error_code": "NOT_FOUND",
  "message": "File 'system1/basic.py' does not exist in the repository.",
  "remediation": "Check the file name. Did you mean 'system1/base.py'? Run list_dir on 'system1' to see all files."
}
```

#### Contoh Respons Actionable Error (Input di Luar Batas):
```json
{
  "status": "error",
  "error_code": "INVALID_ARGUMENT",
  "message": "Parameter 'start_line' must be greater than or equal to 1, received: 0.",
  "remediation": "Line numbers in BrainFrog are 1-indexed. Specify start_line=1 to read from the beginning."
}
```

---

## Referensi Riset & Literatur

Sumber primer yang mendasari penyusunan pedoman arsitektur dan kontrak di repositori ini:

### Pilar System Design
1. **Google Cloud Architecture Framework: System Design**
   - Fokus: Prinsip modularitas, perubahan atomik, dokumentasi arsitektur, dan evaluasi gap analysis berbasis pilar.
   - URL: `https://cloud.google.com/architecture/framework/system-design`
   - Tanggal Akses: 24 September 2026.

2. **AWS Well-Architected Framework: General Design Principles & Trade-Off Evaluation**
   - Fokus: Pengambilan keputusan berbasis data, pengujian pada skala produksi, arsitektur evolusioner, dan trade-off lintas pilar (PERF01-BP04).
   - URL: `https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html`
   - Tanggal Akses: 24 September 2026.

3. **Microsoft Azure Well-Architected Framework: Managing Architecture Trade-Offs**
   - Fokus: Kompromi antar-pilar (Reliability vs Cost, Performance vs Operational Complexity), mitigasi blast radius, dan penggunaan ADR untuk mencatat justifikasi bisnis.
   - URL: `https://learn.microsoft.com/en-us/azure/well-architected/`
   - Tanggal Akses: 24 September 2026.

4. **Documenting Architecture Decisions — Michael Nygard (2011)**
   - Fokus: Format ADR ringan (Context, Decision, Status, Consequences) untuk menjaga rekam jejak keputusan arsitektural penting tanpa dokumen birokratis besar.
   - URL: `https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions`
   - Tanggal Akses: 24 September 2026.

5. **Architecture Decision Records & Context Anchoring — Martin Fowler**
   - Fokus: ADR sebagai instrumen penalaran kontekstual bagi tim rekayasa perangkat lunak dan kolaborasi agen cerdas.
   - URL: `https://martinfowler.com/articles/`
   - Tanggal Akses: 24 September 2026.

6. **You Aren't Gonna Need It (YAGNI) & Monolith First — Martin Fowler**
   - Fokus: Menghindari kompleksitas prematur, memprioritaskan arsitektur sederhana berbasis iterasi cepat, dan menolak modularisasi mikro sebelum ada kebutuhan nyata.
   - URL: `https://martinfowler.com/bliki/Yagni.html`
   - Tanggal Akses: 24 September 2026.

7. **Building Evolutionary Architectures — Neal Ford, Rebecca Parsons, Patrick Kua (Thoughtworks)**
   - Fokus: Konsep *architectural fitness functions* sebagai uji otomatis untuk memvalidasi karakteristik arsitektur sepanjang siklus hidup sistem.
   - URL: `https://www.thoughtworks.com/books/building-evolutionary-architectures`
   - Tanggal Akses: 24 September 2026.

8. **Type 1 and Type 2 Decisions (One-Way vs Two-Way Doors) — Jeff Bezos / Amazon Shareholder Letter**
   - Fokus: Pembedaan keputusan reversibel yang mengutamakan kecepatan versus keputusan struktural yang membutuhkan kehati-hatian tinggi.
   - URL: `https://www.aboutamazon.com/news/company-news/2015-letter-to-shareholders`
   - Tanggal Akses: 24 September 2026.

### Pilar Tool & Contract Design
9. **Model Context Protocol (MCP) Tools Specification — Anthropic / MCP Working Group**
   - Fokus: Spesifikasi standar definisi tool (`name`, `description`, `inputSchema` berbasis JSON Schema 2020-12), penanganan error terstruktur (`isError`), serta panduan penyusunan deskripsi untuk pemahaman model LLM.
   - URL: `https://spec.modelcontextprotocol.io/specification/server/tools/`
   - Tanggal Akses: 24 September 2026.

10. **OpenAPI Specification v3.1.0 — OpenAPI Initiative (Linux Foundation)**
    - Fokus: Penyelarasan penuh dengan JSON Schema 2020-12, pemisahan metode baca vs mutasi, validasi parameter batas, dan pemetaan respons status.
    - URL: `https://spec.openapis.org/oas/v3.1.0`
    - Tanggal Akses: 24 September 2026.

11. **JSON Schema Specification (Draft 2020-12)**
    - Fokus: Validasi struktural berbasis tipe data (`type`, `properties`, `required`, `additionalProperties`), batasan numerik, format, dan enumerasi.
    - URL: `https://json-schema.org/draft/2020-12/release-notes`
    - Tanggal Akses: 24 September 2026.

12. **Google Cloud API Design Guide: API Improvement Proposals (AIP)**
    - Fokus:
      - *AIP-132 / AIP-158:* Standarisasi metode List dan penanganan paginasi data besar.
      - *AIP-134:* Operasi Update berbasis field mask dan definisi semantik idempotensi.
      - *AIP-180:* Aturan kompatibilitas mundur (*backward compatibility*) untuk mencegah breaking changes.
      - *AIP-193:* Standar error informatif dan *actionable* (`error_code`, `message`, dan `details`).
    - URL: `https://google.aip.dev/`
    - Tanggal Akses: 24 September 2026.

13. **RFC 9110: HTTP Semantics — Internet Engineering Task Force (IETF)**
    - Fokus: Definisi formal *Safe Methods* (bebas efek samping) dan *Idempotent Methods* untuk konsistensi kontrak transmisi data dan keandalan pemanggilan ulang.
    - URL: `https://www.rfc-editor.org/rfc/rfc9110.html`
    - Tanggal Akses: 24 September 2026.

