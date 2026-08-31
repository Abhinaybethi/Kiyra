'use client';

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Bot, Sparkles, Sliders, Briefcase, Play, Layers } from 'lucide-react';
import { api, JobDescription, Profile } from '@/lib/api';

const interviewTypes = [
  { id: 'mixed', label: 'Mixed Round', desc: 'Combines technical, system design, and behavioral questions.' },
  { id: 'technical', label: 'Technical Depth', desc: 'Algorithms, language internals, frameworks, and architecture.' },
  { id: 'behavioral', label: 'Behavioral (STAR)', desc: 'Leadership, team conflict, production incidents, and achievements.' },
  { id: 'system_design', label: 'System Design', desc: 'Scalability, microservices, databases, caching, and tradeoffs.' },
  { id: 'coding', label: 'Coding & Logic', desc: 'Data structures, algorithm complexity, and edge cases.' },
  { id: 'hr', label: 'HR & Cultural', desc: 'Motivation, career progression, salary expectations, and fit.' },
];

const difficulties = [
  { id: 'easy', label: 'Junior / Foundational' },
  { id: 'medium', label: 'Mid / Senior Standard' },
  { id: 'hard', label: 'Staff / Principal Lead' },
];

export default function PracticeLaunchPage() {
  const router = useRouter();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [jobs, setJobs] = useState<JobDescription[]>([]);
  const [selectedType, setSelectedType] = useState('mixed');
  const [selectedDifficulty, setSelectedDifficulty] = useState('medium');
  const [selectedJobId, setSelectedJobId] = useState<number | undefined>();
  const [targetRole, setTargetRole] = useState('');
  const [questionCount, setQuestionCount] = useState(5);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getProfile().then((p) => {
      if (p) {
        setProfile(p);
        setTargetRole(p.target_role || '');
      }
    }).catch(() => {});

    api.listJobs().then((jList) => {
      setJobs(jList);
      const active = jList.find((j) => j.is_active);
      if (active) setSelectedJobId(active.id);
    }).catch(() => {});
  }, []);

  const handleStart = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const session = await api.createSession({
        interview_type: selectedType,
        mode: 'practice',
        difficulty: selectedDifficulty,
        target_role: targetRole || profile?.target_role || 'Software Engineer',
        question_count: Number(questionCount),
        job_description_id: selectedJobId,
        title: `${selectedType.toUpperCase()} Mock Interview`,
      });

      router.push(`/practice/${session.id}`);
    } catch (err: any) {
      setError(err.message || 'Failed to start interview');
      setLoading(false);
    }
  };

  return (
    <div className="max-w-4xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="space-y-2">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-xs font-semibold text-indigo-400">
          <Bot className="w-3.5 h-3.5" />
          <span>Interactive AI Mock Simulation</span>
        </div>
        <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Configure Practice Interview</h1>
        <p className="text-sm text-slate-400">
          The AI will simulate an interviewer, adapt questions based on your responses, and evaluate your performance at the end.
        </p>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
          {error}
        </div>
      )}

      <form onSubmit={handleStart} className="space-y-8">
        {/* 1. Interview Type */}
        <div className="space-y-3">
          <label className="text-sm font-semibold text-slate-200 flex items-center gap-2">
            <Layers className="w-4 h-4 text-indigo-400" />
            <span>Select Interview Domain</span>
          </label>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {interviewTypes.map((type) => (
              <button
                type="button"
                key={type.id}
                onClick={() => setSelectedType(type.id)}
                className={`p-4 rounded-xl text-left border transition-all ${
                  selectedType === type.id
                    ? 'bg-indigo-600/15 border-indigo-500 text-white shadow-sm'
                    : 'bg-slate-900/60 border-slate-800 text-slate-300 hover:border-slate-700'
                }`}
              >
                <p className="text-sm font-bold">{type.label}</p>
                <p className="text-xs text-slate-400 mt-1 leading-relaxed">{type.desc}</p>
              </button>
            ))}
          </div>
        </div>

        {/* 2. Target Role & Difficulty */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-200 flex items-center gap-2">
              <Briefcase className="w-4 h-4 text-indigo-400" />
              <span>Target Role</span>
            </label>
            <input
              type="text"
              value={targetRole}
              onChange={(e) => setTargetRole(e.target.value)}
              placeholder="e.g. Senior Backend Engineer"
              className="w-full px-4 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
            />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-200 flex items-center gap-2">
              <Sliders className="w-4 h-4 text-indigo-400" />
              <span>Difficulty Level</span>
            </label>
            <select
              value={selectedDifficulty}
              onChange={(e) => setSelectedDifficulty(e.target.value)}
              className="w-full px-4 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
            >
              {difficulties.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 3. Job Description Link & Question Count */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-200">Linked Job Description (Optional)</label>
            <select
              value={selectedJobId || ''}
              onChange={(e) => setSelectedJobId(e.target.value ? Number(e.target.value) : undefined)}
              className="w-full px-4 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
            >
              <option value="">None (Generic Role Mock)</option>
              {jobs.map((j) => (
                <option key={j.id} value={j.id}>
                  {j.title} {j.company ? `at ${j.company}` : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-200">Number of Questions</label>
            <select
              value={questionCount}
              onChange={(e) => setQuestionCount(Number(e.target.value))}
              className="w-full px-4 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
            >
              <option value={3}>3 Questions (Quick Sprint)</option>
              <option value={5}>5 Questions (Standard Round)</option>
              <option value={8}>8 Questions (In-Depth Technical)</option>
              <option value={10}>10 Questions (Full Marathon)</option>
            </select>
          </div>
        </div>

        {/* Submit */}
        <div className="pt-4 border-t border-slate-800 flex justify-end">
          <button
            type="submit"
            disabled={loading}
            className="flex items-center gap-2 px-6 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-sm transition-all shadow-lg shadow-indigo-600/30 disabled:opacity-50"
          >
            {loading ? (
              <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              <Play className="w-4 h-4 fill-white" />
            )}
            <span>Begin Mock Interview</span>
          </button>
        </div>
      </form>
    </div>
  );
}
