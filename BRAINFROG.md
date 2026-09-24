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

## Referensi Riset & Literatur

Sumber primer yang mendasari penyusunan pedoman System Design ini:

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
