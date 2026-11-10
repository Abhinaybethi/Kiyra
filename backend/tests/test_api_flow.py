"""API integration flow tests."""
import pytest
from unittest.mock import AsyncMock, patch


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_profile_creation_and_retrieval(client):
    # Get profile (initially None)
    res = client.get("/api/profile")
    assert res.status_code == 200

    # Create profile
    payload = {
        "name": "Jane Doe",
        "target_role": "Senior Full-Stack Engineer",
        "experience_level": "senior",
        "years_of_experience": 5.5,
        "preferred_technologies": ["Python", "FastAPI", "React", "TypeScript"],
        "summary": "Experienced engineer with a focus on scalable systems.",
    }
    create_res = client.post("/api/profile", json=payload)
    assert create_res.status_code == 200
    data = create_res.json()
    assert data["name"] == "Jane Doe"
    assert data["target_role"] == "Senior Full-Stack Engineer"

    # Get updated profile
    get_res = client.get("/api/profile")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Jane Doe"


def test_job_description_crud(client):
    payload = {
        "title": "Staff Backend Engineer",
        "company": "Tech Corp",
        "raw_text": "We are looking for a Staff Backend Engineer with Python, PostgreSQL, and Kubernetes experience.",
    }
    with patch("agents.jd_agent.JobDescriptionAgent.run", new_callable=AsyncMock) as mock_agent_run:
        mock_agent_run.return_value.data = {"competency_map": {"technical": ["Python", "PostgreSQL"]}}
        res = client.post("/api/jobs", json=payload)
        assert res.status_code == 200
        jd = res.json()
        assert jd["title"] == "Staff Backend Engineer"
        assert jd["company"] == "Tech Corp"

        # List jobs
        list_res = client.get("/api/jobs")
        assert list_res.status_code == 200
        assert len(list_res.json()) >= 1


def test_activate_job_description_switches_the_active_one(client, db_session):
    from api.profile import get_profile
    from db.models import JobDescription

    profile = get_profile(db_session)
    first = JobDescription(profile_id=profile.id, title="First",
                           raw_text="first role", is_active=True)
    second = JobDescription(profile_id=profile.id, title="Second",
                            raw_text="second role", is_active=False)
    db_session.add_all([first, second])
    db_session.commit()
    db_session.refresh(first)
    db_session.refresh(second)

    res = client.post(f"/api/jobs/{second.id}/activate")
    assert res.status_code == 200
    assert res.json()["id"] == second.id
    assert res.json()["is_active"] is True

    db_session.expire_all()
    assert db_session.get(JobDescription, first.id).is_active is False
    assert db_session.get(JobDescription, second.id).is_active is True


def test_activate_missing_job_description_is_404(client):
    res = client.post("/api/jobs/999999/activate")
    assert res.status_code == 404


def test_interview_session_creation(client):
    payload = {
        "interview_type": "technical",
        "mode": "practice",
        "difficulty": "medium",
        "target_role": "Backend Engineer",
        "question_count": 3,
        "title": "Backend Practice Interview",
    }
    res = client.post("/api/interviews", json=payload)
    assert res.status_code == 200
    session = res.json()
    assert session["status"] == "pending"
    assert session["question_count"] == 3


def test_analytics_dashboard(client):
    res = client.get("/api/analytics/dashboard")
    assert res.status_code == 200
    dash = res.json()
    assert "total_interviews" in dash
    assert "completed_interviews" in dash
    assert "recent_sessions" in dash


def test_settings_models(client):
    res = client.get("/api/settings/models")
    assert res.status_code == 200
    config = res.json()
    assert "provider" in config
    assert "model_name" in config
