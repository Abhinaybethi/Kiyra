/**
 * Centralized API Client for AI Interview Assistance Platform
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
export const WS_BASE = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000';

export interface Profile {
  id: number;
  user_id: number;
  name: string;
  target_role?: string;
  experience_level?: string;
  years_of_experience?: number;
  preferred_technologies?: string;
  summary?: string;
  created_at: string;
  updated_at: string;
}

export interface JobDescription {
  id: number;
  profile_id: number;
  title: string;
  company?: string;
  raw_text: string;
  parsed_data?: {
    seniority_level?: string;
    role_type?: string;
    required_skills?: string[];
    preferred_skills?: string[];
    responsibilities?: string[];
    likely_interview_areas?: string[];
  };
  competency_map?: Record<string, string[]>;
  is_active: boolean;
  created_at: string;
}

export interface Resume {
  id: number;
  profile_id: number;
  filename: string;
  raw_text?: string;
  parsed_data?: {
    name?: string;
    email?: string;
    phone?: string;
    summary?: string;
    skills?: Array<{ name: string; category: string; proficiency: string }>;
    experience?: Array<{ company: string; title: string; start_date: string; end_date: string; description: string; technologies?: string[] }>;
    education?: Array<{ institution: string; degree: string; field: string; graduation_year: string }>;
    projects?: Array<{ name: string; description: string; technologies: string[]; outcomes?: string }>;
  };
  is_active: boolean;
  created_at: string;
}

export interface InterviewSession {
  id: number;
  profile_id: number;
  job_description_id?: number;
  title?: string;
  interview_type: string;
  mode: string;
  difficulty?: string;
  target_role?: string;
  target_tech?: string;
  question_count: number;
  status: 'pending' | 'in_progress' | 'completed' | 'abandoned';
  started_at?: string;
  ended_at?: string;
  created_at: string;
}

export interface Question {
  id: number;
  session_id: number;
  content: string;
  question_type: string;
  order_index: number;
  is_follow_up: boolean;
  ai_classification?: {
    question_type?: string;
    intent?: string;
    technical_topic?: string;
    behavioral_topic?: string;
    suggested_approach?: string;
  };
  asked_at: string;
}

export interface Feedback {
  id: number;
  session_id: number;
  overall_score?: number;
  technical_score?: number;
  communication_score?: number;
  confidence_score?: number;
  relevance_score?: number;
  strengths?: string;
  weaknesses?: string;
  missed_opportunities?: string;
  recommended_topics?: string;
  improvement_plan?: string;
  raw_analysis?: {
    per_question_feedback?: Array<{
      question: string;
      score: number;
      feedback: string;
      star_used?: boolean;
    }>;
  };
  created_at: string;
}

export interface DashboardData {
  total_interviews: number;
  completed_interviews: number;
  avg_overall_score?: number;
  avg_technical_score?: number;
  avg_communication_score?: number;
  profile_name?: string;
  target_role?: string;
  recent_sessions: Array<{
    id: number;
    title: string;
    type: string;
    status: string;
    created_at: string;
    overall_score?: number;
  }>;
  score_trend: Array<{
    date: string;
    score?: number;
    type: string;
  }>;
}

export interface ModelConfig {
  id: number;
  provider: string;
  model_name: string;
  embedding_model: string;
  transcription_model: string;
  provider_url?: string;
  is_active: boolean;
}

export interface KnowledgeDocument {
  id: number;
  title: string;
  source_type: string;
  source_id?: number;
  content_preview: string;
  chunk_count: number;
  metadata?: any;
  created_at?: string;
}

export interface KnowledgeStats {
  profile_id: number;
  total_documents: number;
  total_chunks: number;
  embedding_model: string;
  embedding_dimension: number;
  vector_store: string;
  storage_path: string;
}

export interface SearchKnowledgeResult {
  query: string;
  total_matches: number;
  results: Array<{
    content: string;
    score: number;
    metadata?: any;
  }>;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });

  if (!res.ok) {
    let errorDetail = `Request failed: ${res.statusText}`;
    try {
      const errJson = await res.json();
      errorDetail = errJson.detail || errorDetail;
    } catch {
      // fallback
    }
    throw new Error(errorDetail);
  }

  return res.json();
}

export const api = {
  // Profile
  getProfile: () => request<Profile | null>('/api/profile'),
  createProfile: (data: Partial<Profile>) => request<Profile>('/api/profile', { method: 'POST', body: JSON.stringify(data) }),
  updateProfile: (data: Partial<Profile>) => request<Profile>('/api/profile', { method: 'PATCH', body: JSON.stringify(data) }),

  // Resumes
  listResumes: () => request<Resume[]>('/api/resume'),
  getResume: (id: number) => request<Resume>(`/api/resume/${id}`),
  uploadResume: async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${API_BASE}/api/resume`, {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Upload failed');
    }
    return res.json() as Promise<Resume>;
  },
  deleteResume: (id: number) => request<{ ok: boolean }>(`/api/resume/${id}`, { method: 'DELETE' }),

  // Job Descriptions
  listJobs: () => request<JobDescription[]>('/api/jobs'),
  getJob: (id: number) => request<JobDescription>(`/api/jobs/${id}`),
  createJob: (data: { title: string; company?: string; raw_text: string }) =>
    request<JobDescription>('/api/jobs', { method: 'POST', body: JSON.stringify(data) }),
  deleteJob: (id: number) => request<{ ok: boolean }>(`/api/jobs/${id}`, { method: 'DELETE' }),

  // Knowledge Base
  listKnowledge: () => request<KnowledgeDocument[]>('/api/knowledge'),
  createKnowledge: (data: { title: string; content: string; source_type?: string; metadata?: any }) =>
    request<KnowledgeDocument>('/api/knowledge', { method: 'POST', body: JSON.stringify(data) }),
  deleteKnowledge: (id: number) => request<{ ok: boolean; deleted_id: number }>(`/api/knowledge/${id}`, { method: 'DELETE' }),
  searchKnowledge: (query: string, top_k = 5, min_score = 0.0) =>
    request<SearchKnowledgeResult>('/api/knowledge/search', {
      method: 'POST',
      body: JSON.stringify({ query, top_k, min_score }),
    }),
  getKnowledgeStats: () => request<KnowledgeStats>('/api/knowledge/stats'),

  // Interviews
  listSessions: () => request<InterviewSession[]>('/api/interviews'),
  getSession: (id: number) => request<InterviewSession>(`/api/interviews/${id}`),
  createSession: (data: {
    interview_type: string;
    mode?: string;
    difficulty?: string;
    target_role?: string;
    target_tech?: string[];
    question_count?: number;
    job_description_id?: number;
    title?: string;
  }) => request<InterviewSession>('/api/interviews', { method: 'POST', body: JSON.stringify(data) }),
  startSession: (id: number) => request<Question>(`/api/interviews/${id}/start`, { method: 'POST' }),
  getQuestions: (id: number) => request<Question[]>(`/api/interviews/${id}/questions`),
  submitAnswer: (sessionId: number, questionId: number, content: string, method = 'text') =>
    request<{ response_id: number; is_complete: boolean; next_question?: Question }>(
      `/api/interviews/${sessionId}/questions/${questionId}/answer`,
      { method: 'POST', body: JSON.stringify({ content, method }) }
    ),
  completeSession: (id: number) => request<{ feedback: any; session_id: number }>(`/api/interviews/${id}/complete`, { method: 'POST' }),
  getFeedback: (id: number) => request<Feedback>(`/api/interviews/${id}/feedback`),
  deleteSession: (id: number) => request<{ ok: boolean }>(`/api/interviews/${id}`, { method: 'DELETE' }),
  suggestAnswer: (question: string, question_type = 'unknown', sessionId?: number) =>
    request<{
      answer?: string;
      key_points?: string[];
      star?: { situation?: string; task?: string; action?: string; result?: string };
      follow_up_questions?: string[];
      confidence?: number;
      missing_context?: string;
    }>('/api/interviews/suggest-answer', {
      method: 'POST',
      body: JSON.stringify({ question, question_type, session_id: sessionId }),
    }),

  // Analytics
  getDashboard: () => request<DashboardData>('/api/analytics/dashboard'),
  getSessionAnalytics: (id: number) => request<any>(`/api/analytics/session/${id}`),

  // Settings & Models
  getModelConfig: () => request<ModelConfig>('/api/settings/models'),
  updateModelConfig: (data: Partial<ModelConfig>) =>
    request<ModelConfig>('/api/settings/models', { method: 'PATCH', body: JSON.stringify(data) }),
  getAvailableModels: () => request<{ ollama_available: boolean; models: string[]; error?: string }>('/api/settings/models/available'),
  getModelHealth: () => request<{ healthy: boolean; provider: string; model: string }>('/api/settings/models/health'),
  getSettings: () => request<Record<string, string>>('/api/settings'),
  updateSetting: (key: string, value: string) =>
    request<{ ok: boolean }>(`/api/settings/${key}`, { method: 'PUT', body: JSON.stringify({ value }) }),
};
