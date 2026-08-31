'use client';

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Radio, Mic, ShieldCheck, Briefcase, Play, Sparkles } from 'lucide-react';
import { api, JobDescription, Profile } from '@/lib/api';

export default function LiveAssistantLauncher() {
  const router = useRouter();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [jobs, setJobs] = useState<JobDescription[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<number | undefined>();
  const [title, setTitle] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getProfile().then((p) => {
      if (p) setProfile(p);
    }).catch(() => {});

    api.listJobs().then((jList) => {
      setJobs(jList);
      const active = jList.find((j) => j.is_active);
      if (active) setSelectedJobId(active.id);
    }).catch(() => {});
  }, []);

  const handleLaunch = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const session = await api.createSession({
        interview_type: 'mixed',
        mode: 'live',
        target_role: profile?.target_role || 'Software Engineer',
        job_description_id: selectedJobId,
        title: title || 'Live Real-Time Assistance Session',
      });

      router.push(`/live/${session.id}`);
    } catch (err: any) {
      setError(err.message || 'Failed to initialize live assistant session');
      setLoading(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="space-y-2">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-rose-500/10 border border-rose-500/20 text-xs font-semibold text-rose-400">
          <Radio className="w-3.5 h-3.5 animate-pulse" />
          <span>Real-Time In-Interview Intelligence</span>
        </div>
        <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Launch Live Interview Assistant</h1>
        <p className="text-sm text-slate-400">
          Connect your microphone for live streaming speech-to-text. The AI detects interviewer questions and generates concise STAR answers and technical hints in real time.
        </p>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
          {error}
        </div>
      )}

      {/* Feature Pillars */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="glass-panel p-4 rounded-xl border border-slate-800 space-y-2">
          <Mic className="w-5 h-5 text-indigo-400" />
          <h3 className="text-sm font-semibold text-white">Live Microphone STT</h3>
          <p className="text-xs text-slate-400">
            Local Whisper transcription captures interviewer questions with zero audio leakage.
          </p>
        </div>

        <div className="glass-panel p-4 rounded-xl border border-slate-800 space-y-2">
          <Sparkles className="w-5 h-5 text-purple-400" />
          <h3 className="text-sm font-semibold text-white">Low-Cognitive Load UI</h3>
          <p className="text-xs text-slate-400">
            Concise bullet points, STAR highlights, and architecture trade-offs ready at a glance.
          </p>
        </div>

        <div className="glass-panel p-4 rounded-xl border border-slate-800 space-y-2">
          <ShieldCheck className="w-5 h-5 text-emerald-400" />
          <h3 className="text-sm font-semibold text-white">Manual Fallback</h3>
          <p className="text-xs text-slate-400">
            1-click manual trigger ensures you can query candidate experience at any second.
          </p>
        </div>
      </div>

      <form onSubmit={handleLaunch} className="space-y-6 glass-panel p-6 rounded-2xl border border-slate-800">
        <div className="space-y-2">
          <label className="text-sm font-semibold text-slate-200">Session Name / Company</label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Stripe Senior Backend Live Interview"
            className="w-full px-4 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
          />
        </div>

        <div className="space-y-2">
          <label className="text-sm font-semibold text-slate-200 flex items-center gap-2">
            <Briefcase className="w-4 h-4 text-indigo-400" />
            <span>Target Job Description (for RAG Context)</span>
          </label>
          <select
            value={selectedJobId || ''}
            onChange={(e) => setSelectedJobId(e.target.value ? Number(e.target.value) : undefined)}
            className="w-full px-4 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
          >
            <option value="">None (Use Candidate Resume Context Only)</option>
            {jobs.map((j) => (
              <option key={j.id} value={j.id}>
                {j.title} {j.company ? `at ${j.company}` : ''}
              </option>
            ))}
          </select>
        </div>

        <div className="pt-4 border-t border-slate-800 flex justify-end">
          <button
            type="submit"
            disabled={loading}
            className="flex items-center gap-2 px-6 py-3 rounded-xl bg-rose-600 hover:bg-rose-500 text-white font-semibold text-sm transition-all shadow-lg shadow-rose-600/30 disabled:opacity-50"
          >
            {loading ? (
              <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              <Play className="w-4 h-4 fill-white" />
            )}
            <span>Open Live Assistant Workspace</span>
          </button>
        </div>
      </form>
    </div>
  );
}
