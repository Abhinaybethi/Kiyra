# Kiyra 
#AI Interview Intelligence Platform

A production-grade, local-first, free-first AI interview assistance platform for candidates preparing for and navigating real-time interviews.

![Local-First](https://img.shields.io/badge/Architecture-Local--First-blue.svg)
![AI-Powered](https://img.shields.io/badge/AI-Bonsai%20%7C%20Whisper-orange.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

---

## 🌟 Key Features

1. **Practice Mode (Mock Interviews)**
   - AI acts as an adaptive interviewer across Technical, Behavioral (STAR), Coding, System Design, and HR formats.
   - Real-time voice or text candidate responses with per-question and end-of-interview feedback.
   - Comprehensive performance evaluation (Overall, Technical, Communication, Confidence, and Relevance scores).

2. **Live Assistance Mode**
   - 3-panel low-cognitive-load live workspace:
     - **Left**: Live microphone stream with real-time speech-to-text transcription via `faster-whisper`.
     - **Center**: Question detection & AI answer recommendations (STAR framing, technical trade-offs, supporting points).
     - **Right**: Candidate context, skills, projects, and manual trigger controls.
   - Heuristic question detection with mandatory manual "Generate Answer" trigger fallback.
   - **Interview Listening Mode**: you own the question boundary. Speech accumulates into a turn, only an explicit SEND submits it (a repeated SEND is ignored while generating), and the answer auto-re-arms to listening with the turn rotated — no re-pressing START between questions. Works in the web `/live` workspace and the desktop overlay.

3. **Candidate Knowledge Base & RAG**
   - Local document ingestion for resumes (PDF, DOCX, TXT) and job descriptions.
   - Chunking (512 tokens with 50-token overlap) and local embeddings via `sentence-transformers` (`all-MiniLM-L6-v2`).
   - Persistent vector retrieval using **ChromaDB** to ensure answers are strictly personalized to your actual experience without hallucinating fake credentials.
   - **GitHub-grounded project retrieval** (optional, `KIYRA_GITHUB_ENABLED`): answer questions about your actual repositories using a fine-grained read-only `GITHUB_TOKEN`. Only project-specific questions trigger retrieval — generic tech questions never leave the local models. Single- and multi-project config in `.env.example`.

4. **Centralized Model Abstraction Layer**
   - Zero hardcoded models: the default is local **Bonsai** (llama.cpp `llama-server`, an 8B-class GGUF served OpenAI-compatibly on :8080, GPU-first with a proven CPU fallback, auto-started by the desktop overlay). Switch to Ollama (legacy) or any OpenAI-compatible endpoint directly in Settings.

5. **Analytics & Performance Tracking**
   - Real analytics computed from database session history: score trends, completion rates, strengths, weaknesses, and improvement roadmaps.

---

## 🏗️ Architecture

```
/
├── frontend/               # Next.js 14 (App Router) + TypeScript + Tailwind CSS
│   ├── src/app/            # App routes (Dashboard, Practice, Live, Resume, Jobs, Analytics, Settings)
│   ├── src/components/     # UI components & Navigation
│   └── src/lib/            # API client & WebSocket hooks
│
├── backend/                # FastAPI (Python 3.13+) async backend
│   ├── agents/             # Multi-agent implementations (Resume, JD, Question, Answer, Coach, Orchestrator)
│   ├── ai/                 # Unified AIProvider abstraction (Bonsai, Ollama, OpenAI-compatible)
│   ├── api/                # REST endpoints (/profile, /resume, /jobs, /interviews, /analytics, /settings)
│   ├── db/                 # SQLAlchemy 2.0 ORM models & Alembic migrations
│   ├── knowledge/          # Chunking, local embeddings, and ChromaDB vector store
│   ├── realtime/           # WebSocket session manager & faster-whisper STT
│   ├── github_repo/        # GitHub-grounded project retrieval (provider, indexer, retriever)
│   ├── vision/             # Screenshot/OCR question capture
│   ├── services/           # Document parsing & security validation
│   └── tests/              # Pytest unit & integration test suite
│
├── desktop/                # Windows overlay + backend/model auto-start launchers
├── docker-compose.yml      # Multi-container setup (Backend, Frontend, Ollama)
├── Makefile                # Dev automation commands
└── .env.example            # Environment template
```

---

## 🚀 Quickstart

### Prerequisites
- **Python 3.13+**
- **Node.js 18+** & npm
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- (Legacy only) [Ollama](https://ollama.ai) if you set `AI_PROVIDER=ollama`

### 1. Bonsai (Local AI) — auto-started for you
The desktop overlay starts Bonsai's `llama-server` automatically
(`desktop/llama_server.py`) whenever nothing answers `/v1/models` on
`127.0.0.1:8080` — CUDA offload first, proven CPU fallback. It loads the GGUF
at `C:\AI\models\Bonsai-8B-Q1_0.gguf` (override with `KIYRA_LLAMA_MODEL`) using
the llama.cpp binaries under `%TEMP%\opencode\llama-bin-*`.

Or run the model server on its own:
```bash
uv run desktop/llama_server.py          # GPU-first, CPU fallback
uv run desktop/llama_server.py --cpu    # force CPU
```

### 2. Backend Setup
```bash
cd backend

# Install dependencies with uv
uv sync

# Run database migrations
uv run alembic upgrade head

# (Optional) Seed realistic demo data
uv run python seed.py

# Start backend server (runs on http://localhost:8000)
uv run uvicorn main:app --reload --port 8000
```

> **Desktop overlay:** you do NOT need to start the backend or Bonsai yourself.
> `python desktop/stealth_overlay.py` checks `GET /health` first, launches the
> backend automatically (hidden console, logged to
> `%TEMP%\opencode\kiyra-backend.log`) if nothing is serving, waits for it to
> become healthy, creates the live session, then opens the overlay.
>
> It also starts **Bonsai's llama-server** (`desktop/llama_server.py`, GPU-first
> with the proven CPU fallback) when `/v1/models` is not answering. Without it,
> session start fails with a 503 and *listening never begins*. A model server or
> backend that was already running is reused, never double-started and never
> stopped by the overlay.
>
> ```bash
> # one command — Bonsai + backend included
> python desktop/stealth_overlay.py
>
> # options
> python desktop/stealth_overlay.py --api-url http://localhost:9000
> python desktop/stealth_overlay.py --no-backend          # never start it
> python desktop/stealth_overlay.py --no-model           # never start Bonsai
> python desktop/stealth_overlay.py --startup-timeout 180
> python desktop/stealth_overlay.py --stop-backend-on-exit
> ```
>
> By default both are left running when the overlay closes (keep-warm), so the
> next start is instant. `--stop-backend-on-exit` stops them and frees the GPU.
>
> Environment overrides: `KIYRA_API_URL`, `KIYRA_BACKEND_PYTHON`,
> `KIYRA_BACKEND_STARTUP_TIMEOUT`, `KIYRA_STOP_BACKEND_ON_EXIT`.
> An already-running backend is reused, never restarted, and never killed on
> exit (only a backend this script started, and only with
> `--stop-backend-on-exit`).

### 3. Frontend Setup
```bash
cd frontend

# Install dependencies
npm install

# Start Next.js dev server (runs on http://localhost:3000)
npm run dev
```

Visit **http://localhost:3000** to start practicing!

---

## 🐳 Docker Deployment

Run the entire platform including Ollama with Docker Compose:

```bash
docker compose up --build
```

---

## 🧪 Testing

Run the automated backend test suite:

```bash
cd backend
uv run pytest tests/ -v
```

---

## 🔒 Security & Privacy

- **100% Local-First**: Your resume, job descriptions, and interview audio recordings never leave your machine by default.
- **Path Traversal & File Validation**: Uploads are sanitized and restricted to safe extensions (`.pdf`, `.docx`, `.txt`) up to 10MB.
- **Zero Committed Secrets**: All credentials and custom provider URLs are managed via `.env`.
