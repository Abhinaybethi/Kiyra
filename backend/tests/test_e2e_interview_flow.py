"""End-to-End Interview Journey Smoke & Integration Test."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from db.models import InterviewStatus


def test_full_candidate_interview_lifecycle(client):
    # Step 1: Create Profile
    profile_payload = {
        "name": "Alex Mercer",
        "target_role": "Staff Distributed Systems Engineer",
        "experience_level": "lead",
        "years_of_experience": 8.0,
        "preferred_technologies": ["Go", "Kubernetes", "gRPC", "PostgreSQL", "Kafka"],
        "summary": "Distributed systems specialist focused on fault tolerance and low-latency APIs.",
    }
    prof_res = client.post("/api/profile", json=profile_payload)
    assert prof_res.status_code == 200
    profile_data = prof_res.json()
    assert profile_data["name"] == "Alex Mercer"

    # Step 2: Add Job Description
    jd_payload = {
        "title": "Staff Platform Engineer",
        "company": "CloudScale Inc",
        "raw_text": "Requirements: 7+ years Go/Rust, Raft consensus, Kafka, Kubernetes, high throughput.",
    }
    with patch("agents.jd_agent.JobDescriptionAgent.run", new_callable=AsyncMock) as mock_jd:
        mock_jd.return_value.data = {
            "required_skills": ["Go", "Raft", "Kafka", "Kubernetes"],
            "responsibilities": ["Design distributed consensus protocols", "Scale event pipelines"],
            "competency_map": {"distributed_systems": ["Raft", "Consensus", "Partitioning"]},
        }
        jd_res = client.post("/api/jobs", json=jd_payload)
        assert jd_res.status_code == 200
        jd_data = jd_res.json()
        assert jd_data["title"] == "Staff Platform Engineer"
        jd_id = jd_data["id"]

    # Step 3: Add Knowledge Base Item
    kb_payload = {
        "title": "Designed Custom Raft Protocol Implementation",
        "content": (
            "Situation: Existing leader election had 5s failover latency.\n"
            "Task: Implement custom pre-vote mechanism to prevent disruption.\n"
            "Action: Implemented pre-vote stage in Go, eliminating spurious elections during network blips.\n"
            "Result: Reduced failover latency to 800ms and achieved zero leader flapping."
        ),
        "source_type": "star_story",
    }
    with patch("api.knowledge.add_chunks") as mock_add_chunks:
        mock_add_chunks.return_value = ["chunk-uuid-1"]
        kb_res = client.post("/api/knowledge", json=kb_payload)
        assert kb_res.status_code == 200
        assert kb_res.json()["chunk_count"] >= 1

    # Step 4: Create Practice Interview Session
    session_payload = {
        "title": "Platform Engineering Deep Dive",
        "interview_type": "technical",
        "mode": "practice",
        "difficulty": "hard",
        "target_role": "Staff Platform Engineer",
        "job_description_id": jd_id,
        "question_count": 2,
    }
    session_res = client.post("/api/interviews", json=session_payload)
    assert session_res.status_code == 200
    session_data = session_res.json()
    session_id = session_data["id"]
    assert session_data["status"] == "pending"

    # Step 5: Start Interview (AI generates Question 1)
    with patch("ai.provider.OllamaProvider.complete", new_callable=AsyncMock) as mock_complete:
        mock_complete.return_value = "Can you explain how the Raft consensus algorithm handles network partitions?"
        start_res = client.post(f"/api/interviews/{session_id}/start")
        assert start_res.status_code == 200
        q1 = start_res.json()
        assert "Raft" in q1["content"]
        q1_id = q1["id"]

    # Step 6: Candidate Answers Question 1
    answer1_payload = {
        "content": (
            "In Raft, when a network partition occurs, the cluster is divided. "
            "The minority partition cannot elect a leader because it lacks quorum. "
            "Only the majority partition can accept log entries and commit them."
        ),
        "method": "text",
    }
    with patch("ai.provider.OllamaProvider.complete", new_callable=AsyncMock) as mock_complete:
        # AI asks Question 2 as follow up
        mock_complete.return_value = "How would you handle split-brain scenarios when the partition heals?"
        ans1_res = client.post(
            f"/api/interviews/{session_id}/questions/{q1_id}/answer",
            json=answer1_payload,
        )
        assert ans1_res.status_code == 200
        ans1_data = ans1_res.json()
        assert ans1_data["is_complete"] is False
        assert ans1_data["next_question"] is not None
        q2_id = ans1_data["next_question"]["id"]

    # Step 7: Candidate Answers Question 2 (Final question)
    answer2_payload = {
        "content": (
            "When the partition heals, the leader with the higher term overwrites uncommitted logs in the minority partition, "
            "ensuring strict linearizability and monotonic commit index consistency."
        ),
        "method": "voice",
    }
    ans2_res = client.post(
        f"/api/interviews/{session_id}/questions/{q2_id}/answer",
        json=answer2_payload,
    )
    assert ans2_res.status_code == 200
    ans2_data = ans2_res.json()
    assert ans2_data["is_complete"] is True

    # Step 8: Complete Interview & Run Coach Agent Evaluation
    with patch("agents.coach_agent.CoachAgent.run", new_callable=AsyncMock) as mock_coach:
        mock_coach.return_value.data = {
            "overall_score": 9.2,
            "technical_score": 9.5,
            "communication_score": 9.0,
            "confidence_score": 9.0,
            "relevance_score": 9.5,
            "strengths": ["Deep grasp of Raft consensus", "Clear explanation of linearizability", "Strong terminology"],
            "weaknesses": ["Could mention lease read optimizations"],
            "improvement_plan": "Excellent technical foundation. Prepare system design whiteboard diagrams.",
        }
        comp_res = client.post(f"/api/interviews/{session_id}/complete")
        assert comp_res.status_code == 200
        assert comp_res.json()["session_id"] == session_id

    # Step 9: Verify Feedback API
    fb_res = client.get(f"/api/interviews/{session_id}/feedback")
    assert fb_res.status_code == 200
    fb_data = fb_res.json()
    assert fb_data["session_id"] == session_id
    assert fb_data["overall_score"] == 9.2
    assert fb_data["technical_score"] == 9.5

    # Step 10: Verify Analytics Dashboard reflects real session
    dash_res = client.get("/api/analytics/dashboard")
    assert dash_res.status_code == 200
    dash_data = dash_res.json()
    assert dash_data["total_interviews"] >= 1
    assert dash_data["completed_interviews"] >= 1
    assert dash_data["avg_overall_score"] is not None
