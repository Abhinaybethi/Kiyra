'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  Bot,
  Radio,
  FileText,
  Briefcase,
  Award,
  TrendingUp,
  Clock,
  ArrowRight,
  Sparkles,
  CheckCircle2,
  AlertCircle,
} from 'lucide-react';
import { api, DashboardData, Profile } from '@/lib/api';

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      api.getDashboard().then(setData).catch(() => null),
      api.getProfile().then(setProfile).catch(() => null),
    ]).finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-slate-400">Loading intelligence dashboard...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Welcome Banner */}
      <div className="relative overflow-hidden rounded-2xl p-6 md:p-8 border border-slate-800 bg-gradient-to-r from-slate-900 via-indigo-950/40 to-slate-900">
        <div className="relative z-10 flex flex-col md:flex-row md:items-center justify-between gap-6">
          <div className="space-y-2">
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-xs font-semibold text-indigo-400">
              <Sparkles className="w-3.5 h-3.5" />
              <span>Ready for Mock Prep & Real-Time Assist</span>
            </div>
            <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">
              Welcome back, {profile?.name || 'Candidate'}
            </h1>
            <p className="text-sm text-slate-400 max-w-xl">
              Targeting{' '}
              <span className="text-indigo-300 font-medium">{profile?.target_role || 'Software Engineering'}</span>.
              Local AI models are synced to your candidate context.
            </p>
          </div>

          <div className="flex flex-wrap gap-3">
            <Link
              href="/practice"
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold transition-all shadow-md shadow-indigo-600/25"
            >
              <Bot className="w-4 h-4" />
              <span>Start Mock Interview</span>
            </Link>
            <Link
              href="/live"
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 text-sm font-semibold transition-all"
            >
              <Radio className="w-4 h-4 text-rose-400" />
              <span>Launch Live Assistant</span>
            </Link>
          </div>
        </div>
      </div>

      {/* Metrics Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="glass-panel p-5 rounded-2xl border border-slate-800/80 space-y-1">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Interviews Completed</span>
            <CheckCircle2 className="w-4 h-4 text-indigo-400" />
          </div>
          <p className="text-2xl font-bold text-white tracking-tight">
            {data?.completed_interviews || 0}
            <span className="text-xs text-slate-500 font-normal ml-1.5">/ {data?.total_interviews || 0} created</span>
          </p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800/80 space-y-1">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Average Overall Score</span>
            <Award className="w-4 h-4 text-amber-400" />
          </div>
          <p className="text-2xl font-bold text-white tracking-tight">
            {data?.avg_overall_score !== null && data?.avg_overall_score !== undefined
              ? `${data.avg_overall_score} / 10`
              : '—'}
          </p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800/80 space-y-1">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Technical Accuracy</span>
            <TrendingUp className="w-4 h-4 text-emerald-400" />
          </div>
          <p className="text-2xl font-bold text-white tracking-tight">
            {data?.avg_technical_score !== null && data?.avg_technical_score !== undefined
              ? `${data.avg_technical_score} / 10`
              : '—'}
          </p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800/80 space-y-1">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Communication & STAR</span>
            <Sparkles className="w-4 h-4 text-purple-400" />
          </div>
          <p className="text-2xl font-bold text-white tracking-tight">
            {data?.avg_communication_score !== null && data?.avg_communication_score !== undefined
              ? `${data.avg_communication_score} / 10`
              : '—'}
          </p>
        </div>
      </div>

      {/* Main Grid: Quick Flow + Recent Interviews */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left 2 Cols: Setup Steps & Quick Actions */}
        <div className="lg:col-span-2 space-y-6">
          <div className="glass-panel p-6 rounded-2xl border border-slate-800">
            <h2 className="text-base font-bold text-white mb-4 flex items-center gap-2">
              <Sparkles className="w-4 h-4 text-indigo-400" />
              <span>Prep Workflow</span>
            </h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <Link
                href="/resume"
                className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 hover:border-indigo-500/40 hover:bg-slate-900 transition-all group"
              >
                <div className="w-9 h-9 rounded-lg bg-blue-500/10 text-blue-400 flex items-center justify-center mb-3 group-hover:scale-110 transition-transform">
                  <FileText className="w-5 h-5" />
                </div>
                <h3 className="text-sm font-semibold text-white group-hover:text-indigo-400 transition-colors">
                  1. Upload Resume
                </h3>
                <p className="text-xs text-slate-400 mt-1">
                  Extract skills, projects, and vectorize your real experience for contextual answers.
                </p>
              </Link>

              <Link
                href="/jobs"
                className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 hover:border-indigo-500/40 hover:bg-slate-900 transition-all group"
              >
                <div className="w-9 h-9 rounded-lg bg-purple-500/10 text-purple-400 flex items-center justify-center mb-3 group-hover:scale-110 transition-transform">
                  <Briefcase className="w-5 h-5" />
                </div>
                <h3 className="text-sm font-semibold text-white group-hover:text-indigo-400 transition-colors">
                  2. Add Job Description
                </h3>
                <p className="text-xs text-slate-400 mt-1">
                  Generate competency maps, required skills, and tailored interview questions.
                </p>
              </Link>

              <Link
                href="/practice"
                className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 hover:border-indigo-500/40 hover:bg-slate-900 transition-all group"
              >
                <div className="w-9 h-9 rounded-lg bg-emerald-500/10 text-emerald-400 flex items-center justify-center mb-3 group-hover:scale-110 transition-transform">
                  <Bot className="w-5 h-5" />
                </div>
                <h3 className="text-sm font-semibold text-white group-hover:text-indigo-400 transition-colors">
                  3. Practice Mock Rounds
                </h3>
                <p className="text-xs text-slate-400 mt-1">
                  Adaptive AI interviewer with voice/text input and instant evaluation.
                </p>
              </Link>

              <Link
                href="/live"
                className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 hover:border-rose-500/40 hover:bg-slate-900 transition-all group"
              >
                <div className="w-9 h-9 rounded-lg bg-rose-500/10 text-rose-400 flex items-center justify-center mb-3 group-hover:scale-110 transition-transform">
                  <Radio className="w-5 h-5" />
                </div>
                <h3 className="text-sm font-semibold text-white group-hover:text-rose-400 transition-colors">
                  4. Live Interview Assist
                </h3>
                <p className="text-xs text-slate-400 mt-1">
                  Real-time microphone transcription with STAR and technical hint generation.
                </p>
              </Link>
            </div>
          </div>
        </div>

        {/* Right Col: Recent Sessions List */}
        <div className="glass-panel p-6 rounded-2xl border border-slate-800 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-base font-bold text-white flex items-center gap-2">
                <Clock className="w-4 h-4 text-slate-400" />
                <span>Recent Sessions</span>
              </h2>
              <Link href="/history" className="text-xs text-indigo-400 hover:underline">
                View all
              </Link>
            </div>

            {data?.recent_sessions && data.recent_sessions.length > 0 ? (
              <div className="space-y-3">
                {data.recent_sessions.map((session) => (
                  <Link
                    key={session.id}
                    href={session.status === 'completed' ? `/history/${session.id}` : `/practice/${session.id}`}
                    className="block p-3 rounded-xl bg-slate-900/70 border border-slate-800/80 hover:border-slate-700 transition-all"
                  >
                    <div className="flex items-center justify-between">
                      <p className="text-xs font-semibold text-slate-200 truncate">{session.title}</p>
                      {session.overall_score && (
                        <span className="text-[11px] font-bold text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded-md">
                          {session.overall_score}/10
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 mt-1.5 text-[11px] text-slate-400">
                      <span className="capitalize">{session.type}</span>
                      <span>•</span>
                      <span className={`capitalize ${session.status === 'completed' ? 'text-emerald-400' : 'text-indigo-400'}`}>
                        {session.status.replace('_', ' ')}
                      </span>
                    </div>
                  </Link>
                ))}
              </div>
            ) : (
              <div className="text-center py-8 text-slate-500 space-y-2">
                <AlertCircle className="w-6 h-6 mx-auto text-slate-600" />
                <p className="text-xs">No interview sessions recorded yet.</p>
                <Link href="/practice" className="inline-block text-xs font-semibold text-indigo-400 hover:underline">
                  Launch your first mock round →
                </Link>
              </div>
            )}
          </div>

          <div className="mt-6 pt-4 border-t border-slate-800/60">
            <Link
              href="/analytics"
              className="flex items-center justify-between text-xs font-medium text-slate-400 hover:text-slate-200 transition-colors"
            >
              <span>View full competency analytics</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
