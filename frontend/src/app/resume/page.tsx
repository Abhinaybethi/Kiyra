'use client';

import React, { useState, useEffect } from 'react';
import {
  FileText,
  UploadCloud,
  CheckCircle2,
  Trash2,
  Sparkles,
  Layers,
  Award,
  Briefcase,
  AlertCircle,
} from 'lucide-react';
import { api, Resume, Profile } from '@/lib/api';

export default function ResumePage() {
  const [resumes, setResumes] = useState<Resume[]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const loadData = () => {
    api.listResumes().then(setResumes).catch(() => {});
    api.getProfile().then(setProfile).catch(() => {});
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setUploading(true);
    setError(null);
    setSuccessMsg(null);

    try {
      await api.uploadResume(file);
      setSuccessMsg('Resume uploaded successfully! AI parsing and ChromaDB vector ingestion started.');
      loadData();
    } catch (err: any) {
      setError(err.message || 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this resume?')) return;
    try {
      await api.deleteResume(id);
      loadData();
    } catch (err: any) {
      setError(err.message || 'Delete failed');
    }
  };

  const activeResume = resumes.find((r) => r.is_active) || resumes[0];

  return (
    <div className="max-w-5xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="space-y-2">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-blue-500/10 border border-blue-500/20 text-xs font-semibold text-blue-400">
          <FileText className="w-3.5 h-3.5" />
          <span>Candidate Intelligence Ingestion</span>
        </div>
        <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Resume & Knowledge Base</h1>
        <p className="text-sm text-slate-400">
          Upload your resume in PDF, DOCX, or TXT format. The multi-agent system extracts structured skills, projects, and generates vector embeddings locally for RAG context retrieval.
        </p>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
          {error}
        </div>
      )}

      {successMsg && (
        <div className="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-sm flex items-center gap-2">
          <CheckCircle2 className="w-4 h-4 text-emerald-400" />
          <span>{successMsg}</span>
        </div>
      )}

      {/* Upload Box */}
      <div className="glass-panel p-8 rounded-2xl border border-dashed border-slate-700 hover:border-indigo-500/50 transition-colors text-center">
        <input
          type="file"
          id="resume-upload"
          accept=".pdf,.docx,.txt"
          onChange={handleFileUpload}
          disabled={uploading}
          className="hidden"
        />
        <label htmlFor="resume-upload" className="cursor-pointer flex flex-col items-center justify-center space-y-3">
          <div className="w-14 h-14 rounded-2xl bg-indigo-500/10 text-indigo-400 flex items-center justify-center border border-indigo-500/20">
            {uploading ? (
              <span className="w-6 h-6 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin" />
            ) : (
              <UploadCloud className="w-7 h-7" />
            )}
          </div>
          <p className="text-sm font-semibold text-white">
            {uploading ? 'Parsing with AI Agent...' : 'Click or drop your resume here'}
          </p>
          <p className="text-xs text-slate-400">PDF, DOCX, or TXT (Max 10MB) • Stored 100% locally</p>
        </label>
      </div>

      {/* Parsed Resume Overview */}
      {activeResume ? (
        <div className="space-y-6">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-bold text-white flex items-center gap-2">
              <Sparkles className="w-5 h-5 text-indigo-400" />
              <span>Extracted Candidate Context</span>
            </h2>
            <button
              onClick={() => handleDelete(activeResume.id)}
              className="flex items-center gap-1.5 text-xs text-rose-400 hover:text-rose-300 px-3 py-1.5 rounded-lg bg-rose-500/10 border border-rose-500/20"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>Delete Resume</span>
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {/* Left Col: Candidate Info & Skills */}
            <div className="space-y-6">
              <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-3">
                <p className="text-xs font-bold text-slate-400 uppercase tracking-wider">Candidate Profile</p>
                <div>
                  <p className="text-base font-bold text-white">{activeResume.parsed_data?.name || profile?.name || 'Candidate'}</p>
                  <p className="text-xs text-slate-400">{activeResume.parsed_data?.email || 'Email in document'}</p>
                </div>
                {activeResume.parsed_data?.summary && (
                  <p className="text-xs text-slate-300 leading-relaxed border-t border-slate-800/80 pt-2">
                    {activeResume.parsed_data.summary}
                  </p>
                )}
              </div>

              {/* Skills Tag Cloud */}
              <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-3">
                <p className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Award className="w-3.5 h-3.5 text-indigo-400" />
                  <span>Indexed Technical Skills</span>
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {activeResume.parsed_data?.skills && activeResume.parsed_data.skills.length > 0 ? (
                    activeResume.parsed_data.skills.map((s, idx) => (
                      <span
                        key={idx}
                        className="px-2.5 py-1 rounded-lg bg-indigo-600/15 border border-indigo-500/30 text-indigo-300 text-xs font-medium"
                      >
                        {s.name}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-slate-500">Skills extraction pending...</span>
                  )}
                </div>
              </div>
            </div>

            {/* Right 2 Cols: Experience & Projects */}
            <div className="md:col-span-2 space-y-6">
              {/* Experience */}
              <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-4">
                <p className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Briefcase className="w-3.5 h-3.5 text-indigo-400" />
                  <span>Work Experience</span>
                </p>
                {activeResume.parsed_data?.experience && activeResume.parsed_data.experience.length > 0 ? (
                  <div className="space-y-4">
                    {activeResume.parsed_data.experience.map((exp, i) => (
                      <div key={i} className="p-3.5 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1.5">
                        <div className="flex items-center justify-between">
                          <p className="text-sm font-bold text-white">{exp.title}</p>
                          <span className="text-xs text-slate-400">
                            {exp.start_date} - {exp.end_date}
                          </span>
                        </div>
                        <p className="text-xs font-semibold text-indigo-400">{exp.company}</p>
                        <p className="text-xs text-slate-300 leading-relaxed">{exp.description}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-slate-500">No structured experience records found.</p>
                )}
              </div>

              {/* Projects */}
              {activeResume.parsed_data?.projects && activeResume.parsed_data.projects.length > 0 && (
                <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-4">
                  <p className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Layers className="w-3.5 h-3.5 text-purple-400" />
                    <span>Key Projects (Vectorized for RAG)</span>
                  </p>
                  <div className="space-y-3">
                    {activeResume.parsed_data.projects.map((proj, i) => (
                      <div key={i} className="p-3.5 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1">
                        <p className="text-sm font-bold text-white">{proj.name}</p>
                        <p className="text-xs text-slate-300 leading-relaxed">{proj.description}</p>
                        {proj.outcomes && (
                          <p className="text-xs text-emerald-400 font-medium">Impact: {proj.outcomes}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="glass-panel p-8 rounded-2xl border border-slate-800 text-center space-y-2 text-slate-500">
          <AlertCircle className="w-8 h-8 mx-auto text-slate-600" />
          <p className="text-sm">No resume indexed yet.</p>
          <p className="text-xs">Upload your resume above to empower RAG context retrieval.</p>
        </div>
      )}
    </div>
  );
}
