.PHONY: dev backend frontend test seed migrate clean

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
