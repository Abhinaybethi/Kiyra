.PHONY: dev backend frontend test live-check seed migrate clean

# Start both backend and frontend concurrently
dev:
	@echo "Starting AI Interview Platform..."
	@echo "Backend -> http://localhost:8000"
	@echo "Frontend -> http://localhost:3000"
	@powershell -Command "Start-Process -NoNewWindow -FilePath 'powershell' -ArgumentList '-Command', 'cd backend; uv run uvicorn main:app --reload --port 8000'; cd frontend; npm run dev"

# Start only backend
backend:
	cd backend && uv run uvicorn main:app --reload --port 8000

# Start only frontend
frontend:
	cd frontend && npm run dev

# Run backend pytest suite
test:
	cd backend && uv run pytest tests/ -v

# Live end-to-end check: real backend + Bonsai + Whisper + Windows SAPI TTS.
# Requires a running backend on :8000. Not part of `make test` (needs a GPU model).
live-check:
	cd backend && uv run python tests/integration/live_continuous.py

# Seed development/demo data
seed:
	cd backend && uv run python seed.py

# Run Alembic migrations
migrate:
	cd backend && uv run alembic upgrade head

# Generate a new migration
migration:
	cd backend && uv run alembic revision --autogenerate -m "$(name)"

# Clean temporary files
clean:
	rm -rf backend/.pytest_cache backend/**/__pycache__ frontend/.next
