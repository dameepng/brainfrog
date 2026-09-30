# BrainFrog CLI 🐸

> CLI agentic coding dengan hybrid System 1 (Jev / keputusan terstruktur cepat) + System 2 (LLM generatif), dilengkapi verifikasi browser otomatis sebelum kode dianggap selesai.

---

## ✨ Apa yang Membuat BrainFrog Berbeda

BrainFrog bukan sekadar pembungkus LLM untuk menghasilkan kode di terminal. Sistem ini dibangun dengan pengawasan ketat dan otomasi verifikasi end-to-end:

- **Verifikasi Browser Otomatis (MCP Quality Gate)** — Untuk setiap perubahan kode frontend, BrainFrog secara otomatis mengompilasi proyek (`npm run build`), menjalankan dev server, membuka browser Chromium nyata via Model Context Protocol (MCP), memeriksa console error, mendeteksi failed network requests (404/500), serta mengambil screenshot halaman sebelum pekerjaan dinyatakan selesai.
- **Hybrid Decision-Making (Dual-System)** — Mengadopsi arsitektur kognitif: **System 1 (Jev / TypeSafe)** bertindak sebagai gatekeeper cepat bertipe deterministik untuk klasifikasi domain, penentuan cabang kegagalan, dan penilaian risiko diff; **System 2 (Generative Brain)** bertugas menangani penalaran mendalam, dekomposisi rencana, dan sintesis kode.
- **PR Proof Abadi (Immutable Screenshot Storage)** — Screenshot visual desktop dan mobile otomatis tersemat di body Pull Request untuk setiap perubahan frontend. Screenshot disimpan di orphan branch terpisah (`pr-proof-assets`) menggunakan exact commit SHA — tautan gambar terjamin abadi (`200 OK`) dan tidak akan rusak meski branch PR dihapus setelah merge.
- **Infrastruktur Hardened & Proteksi Ketat** — Dilengkapi branch protection wajib di `main`, pipeline CI (`lint-typecheck-test`) dengan GitHub Actions yang di-pin ke exact commit SHA (kebal supply-chain attack), pencegahan kebocoran secret (GitGuardian + Git Guard internal), serta pembersihan otomatis PR pengujian yang basi (`stale.yml`) tanpa mengganggu PR kerja aktif.

---

## 🏗️ Arsitektur

```
                                  [Prompt Pengguna]
                                          │
                                          ▼
                            ┌───────────────────────────┐
                            │   System 1: Scope Gate    │
                            │  (Domain Routing & Tipe)  │
                            └───────────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
         [Pertanyaan Saja]                               [Modifikasi Kode]
    System 2 mendiagnosis &                             System 2 membuat rencana
    menjawab pertanyaan user.                           eksekusi multi-langkah.
    (Tanpa modifikasi file)                                       │
                                                                  ▼
                                                    ┌───────────────────────────┐
                                              ┌───► │ Langkah N: Sintesis Kode  │
                                              │     └───────────────────────────┘
                                              │                   │
                                              │                   ▼
                                              │     ┌───────────────────────────┐
                                              │     │ Eksekusi Unit Test Lokal  │
                                              │     └───────────────────────────┘
                                              │                   │
                                              │                   ▼
                                              │     ┌───────────────────────────┐
                                              │     │  Frontend Quality Gate    │
                                              │     │  (MCP Browser Verify)     │
                                              │     └───────────────────────────┘
                                              │                   │
                                              │                   ▼
                                              │     ┌───────────────────────────┐
                                              │     │    System 1: Loop Gate    │
                                              │     │  (Evaluasi Hasil & Next)  │
                                              │     └───────────────────────────┘
                                              │       ├── open_pr ──► [Auto-Attach PR Proof]
                                              └── retry_fix          ├── escalate_human
                                                                     └── abandon
```

### Komponen Utama

| Komponen | Lokasi File | Peran & Tanggung Jawab |
| :--- | :--- | :--- |
| **Orchestrator** | [`orchestrator.py`](orchestrator.py) | *State machine* sentral: orkestrasi siklus eksekusi, eksekusi *subprocess* dengan *process tree termination*, penulisan file atomik, *self-healing retry loop*, dan pelaporan. |
| **Frontend Quality Gate** | [`core/frontend_quality_gate.py`](core/frontend_quality_gate.py) | Pipeline verifikasi browser 7 langkah (build → dev server → navigate → console → network → screenshot → verdict) melalui server MCP. |
| **PR Proof Generator** | [`core/pr_proof.py`](core/pr_proof.py) | Otomasi tangkapan layar desktop & mobile yang diunggah ke orphan branch `pr-proof-assets` via isolated worktree dan diformat menggunakan commit SHA permanen. |
| **MCP Client (Layer 1)** | [`core/mcp_client.py`](core/mcp_client.py) | Klien generic stdio JSON-RPC 2.0 untuk komunikasi dengan server MCP, menangani handshake, manajemen proses, dan pemanggilan tool. |
| **System 1 (Jev)** | [`system1/`](system1/) | Lapisan keputusan terstruktur bertipe (`ChoiceQuestion`, `ScoreQuestion`, `NoulQuestion`) via TypeSafe Jev API tanpa risiko *prompt drift*. |
| **System 2 (Generative)** | [`system2/`](system2/) | Mesin penalaran generatif. Mendukung **Google Antigravity** (`agy` CLI dengan Google Auth session gratis) dan **Anthropic Claude** (Claude Sonnet / Opus via API Key). |
| **Security & Git Guard** | [`security/`](security/) | Pemindaian perubahan *staged* untuk mencegah kebocoran file sensitif (`.env`, token, private keys) dan rotasi auth key. |
| **Stale PR Lifecycle** | [`.github/workflows/stale.yml`](.github/workflows/stale.yml) & [`.github/scripts/protect_active_prs.py`](.github/scripts/protect_active_prs.py) | Pembersihan otomatis PR verifikasi sesaat dengan proteksi otomatis label `keep-open` pada PR kerja aktif. |

---

## 🚀 Instalasi & Cara Pakai

### 1. Prasyarat

- **Python 3.10+** (disarankan Python 3.11 atau lebih baru)
- **Node.js 18+** (diperlukan jika menggunakan server verifikasi browser MCP)
- **Git CLI** dan **GitHub CLI (`gh`)** (diperlukan untuk pembuatan PR otomatis)
- Salah satu dari provider System 2 berikut:
  - **Google Antigravity (`agy` CLI)** dengan sesi login Google Account aktif (*tidak memerlukan API key*), atau
  - **Anthropic API Key** (`ANTHROPIC_API_KEY`)
- **TypeSafe / Jev API Key** (`TYPESAFE_API_KEY`) untuk System 1

### 2. Pemasangan

```bash
# Clone repositori
git clone https://github.com/dameepng/brainfrog.git
cd brainfrog

# Buat virtual environment
python -m venv .venv
source .venv/bin/activate  # Di Windows: .venv\Scripts\activate

# Install dependensi
pip install -r requirements.txt
pip install -e .

# Siapkan konfigurasi environment
cp .env.example .env
```

### 3. Konfigurasi Variabel Lingkungan (`.env`)

Sesuaikan variabel di dalam file `.env`:

```ini
# --- System 2 Provider ---
# Pilihan: antigravity (Google Auth) atau claude (Anthropic API Key)
SYSTEM2_PROVIDER=antigravity
ANTIGRAVITY_MODEL=gemini-3.8-flash-high

# Jika menggunakan Claude:
# ANTHROPIC_API_KEY=your_anthropic_api_key_here
# ANTHROPIC_MODEL=claude-sonnet-5

# --- System 1 Provider (Jev / TypeSafe) ---
TYPESAFE_API_KEY=your_typesafe_api_key_here

# --- Quality Gate & Browser Verification (Opsional) ---
# BRAINFROG_VERIFY_MCP_PATH=/path/to/brainfrog-verify-mcp/dist/index.js
# BRAINFROG_DEV_URL=http://localhost:3000
```

### 4. Menjalankan BrainFrog

#### Mode Interaktif (REPL) — Disarankan
```bash
brainfrog --repo /path/to/your/project
# atau alias singkat
bf --repo /path/to/your/project
```

Perintah cepat di dalam REPL:
- `@path/file.py` — *Autocomplete* dan penyisipan konteks file ke prompt
- `!command` — Eksekusi perintah terminal langsung (misal: `!pytest`)
- `/diff` — Tinjau perubahan git yang belum di-commit
- `/undo` — Revert perubahan file yang baru saja dibuat agent secara bersih
- `/rules` — Tampilkan pedoman aktif dari `BRAINFROG.md`
- `/stats` — Tinjau konsumsi token dan estimasi biaya sesi

#### Mode Non-Interaktif (Single Task)
```bash
# Memperbaiki bug secara langsung dengan pengujian otomatis
brainfrog \
  --repo /path/to/your/project \
  --task "Fix ZeroDivisionError in math_utils.py" \
  --test-cmd "pytest -q" \
  --backend antigravity

# Perubahan frontend dengan auto-PR dan verifikasi visual
brainfrog \
  --repo /path/to/your/project \
  --task "Tambahkan dark mode toggle pada halaman landing" \
  --test-cmd "npm test" \
  --auto-pr
```

---

## 🔒 Quality & Security

Repositori ini menerapkan standar rekayasa software teruji untuk menjamin keandalan dan keamanan:

1. **Strict Pull Request Workflow & Branch Protection**:
   - Branch `main` dilindungi secara ketat. Push langsung ditolak (`GH006`).
   - Aturan `enforce_admins: true` aktif — admin sekalipun wajib melalui mekanisme Pull Request.
2. **Automated CI Validation (`lint-typecheck-test`)**:
   - Setiap PR wajib melewati validasi kompilasi sintaksis (`compileall`), audit modul keamanan (`Git Guard`), dan eksekusi test suite unit tanpa toleransi error.
3. **Pencegahan Kebocoran Secret**:
   - Pemindaian pre-commit/pre-stage internal melalui `security/git_guard.py`.
   - Pemindaian otomatis berkelanjutan oleh GitHub GitGuardian Security Checks pada setiap PR.
4. **Supply Chain Defense (Pinned Action SHAs)**:
   - Seluruh GitHub Actions di-pin ke exact 40-karakter commit SHA (bukan mutable semantic tag) untuk mencegah eksploitasi dependensi CI pihak ketiga.
5. **Penyimpanan Bukti Visual yang Kebal Deletion**:
   - Branch `pr-proof-assets` diatur terpisah sebagai *orphan branch append-only*. Screenshot di-link menggunakan commit SHA permanen, menjamin URL bukti tidak pernah 404 saat branch fitur dihapus.
6. **Automated Stale PR Management**:
   - Workflow pembersih otomatis menutup PR testing/verifikasi sesaat yang tidak aktif, dengan perlindungan otomatis (`keep-open`) untuk PR pekerjaan nyata.

---

## 🤝 Panduan Kontribusi

Kontribusi dari komunitas sangat disambut. Ikuti alur kerja standar berikut:

1. **Fork & Clone**:
   ```bash
   git clone https://github.com/<username>/brainfrog.git
   cd brainfrog
   ```
2. **Setup Lingkungan Pengembangan**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -e .
   ```
3. **Menjalankan Pengujian Lokal**:
   Pastikan seluruh test suite lolos sebelum mengajukan perubahan:
   ```bash
   python -m unittest discover -s tests
   ```
4. **Konvensi Branch & Pengajuan PR**:
   - Buat branch fitur dari `main` dengan awalan deskriptif: `feat/nama-fitur`, `fix/nama-bug`, atau `docs/perubahan`.
   - *Catatan:* Hindari menggunakan awalan `test/` untuk branch kerja aktif, karena awalan tersebut dialokasikan untuk siklus pembersihan PR verifikasi otomatis.
   - Ajukan Pull Request ke branch `main`. Pastikan seluruh status check CI berhasil.
   - **Penting:** Jangan pernah menghapus atau mengubah history pada branch `pr-proof-assets` karena branch tersebut digunakan untuk menyimpan aset pembuktian abadi.

---

## 📄 License

*Status Lisensi:* Repositori ini sedang dalam tahap finalisasi pemilihan lisensi open-source resmi (seperti lisensi MIT). Detail hak cipta dan lisensi lengkap akan segera diperbarui.
