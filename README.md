# Kiyra 
#AI Interview Intelligence Platform

A production-grade, local-first, free-first AI interview assistance platform for candidates preparing for and navigating real-time interviews.

![Local-First](https://img.shields.io/badge/Architecture-Local--First-blue.svg)
![AI-Powered](https://img.shields.io/badge/AI-Ollama%20%7C%20Whisper-orange.svg)
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

3. **Candidate Knowledge Base & RAG**
   - Local document ingestion for resumes (PDF, DOCX, TXT) and job descriptions.
   - Chunking (512 tokens with 50-token overlap) and local embeddings via `sentence-transformers` (`all-MiniLM-L6-v2`).
   - Persistent vector retrieval using **ChromaDB** to ensure answers are strictly personalized to your actual experience without hallucinating fake credentials.

4. **Centralized Model Abstraction Layer**
   - Zero hardcoded models: easily switch between local Ollama models (`llama3.2:3b`, `mistral`, `deepseek-r1`, `qwen2.5-coder`) or OpenAI-compatible endpoints directly in Settings.

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
├── backend/                # FastAPI (Python 3.11+) async backend
│   ├── agents/             # Multi-agent implementations (Resume, JD, Question, Answer, Coach, Orchestrator)
│   ├── ai/                 # Unified AIProvider abstraction (Ollama, OpenAI-compatible)
│   ├── api/                # REST endpoints (/profile, /resume, /jobs, /interviews, /analytics, /settings)
│   ├── db/                 # SQLAlchemy 2.0 ORM models & Alembic migrations
│   ├── knowledge/          # Chunking, local embeddings, and ChromaDB vector store
│   ├── realtime/           # WebSocket session manager & faster-whisper STT
│   ├── services/           # Document parsing & security validation
│   └── tests/              # Pytest unit & integration test suite
│
├── docker-compose.yml      # Multi-container setup (Backend, Frontend, Ollama)
├── Makefile                # Dev automation commands
└── .env.example            # Environment template
```

---

## 🚀 Quickstart

### Prerequisites
- **Python 3.11+**
- **Node.js 18+** & npm
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- [Ollama](https://ollama.ai) (for local free LLM inference)

### 1. Setup Ollama (Local AI)
```bash
# Start Ollama
ollama serve

# Pull default models
ollama pull llama3.2:3b
ollama pull nomic-embed-text
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
