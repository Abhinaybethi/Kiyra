'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import {
  History,
  Trash2,
  ArrowRight,
  Award,
  Calendar,
  AlertCircle,
} from 'lucide-react';
import { api, InterviewSession } from '@/lib/api';

export default function HistoryPage() {
  const [sessions, setSessions] = useState<InterviewSession[]>([]);
  const [filter, setFilter] = useState('all');
  const [loading, setLoading] = useState(true);

  const loadSessions = () => {
    api.listSessions().then(setSessions).finally(() => setLoading(false));
  };

  useEffect(() => {
    loadSessions();
  }, []);

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this session?')) return;
    try {
      await api.deleteSession(id);
      loadSessions();
    } catch {}
  };

  const filteredSessions = sessions.filter((s) => {
    if (filter === 'all') return true;
    return s.interview_type === filter || s.mode === filter;
  });

  return (
    <div className="max-w-5xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-xs font-semibold text-indigo-400">
            <History className="w-3.5 h-3.5" />
            <span>Persistent Interview Logs</span>
          </div>
          <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Interview Session History</h1>
          <p className="text-sm text-slate-400">
            Review past mock rounds, live transcript logs, coach assessments, and improvement areas.
          </p>
        </div>

        {/* Filter */}
        <div className="flex items-center gap-2">
          {['all', 'technical', 'behavioral', 'system_design', 'live'].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium capitalize transition-colors ${
                filter === f
                  ? 'bg-indigo-600 text-white'
                  : 'bg-slate-900 text-slate-400 hover:text-white border border-slate-800'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {/* Session Cards */}
      <div className="space-y-4">
        {filteredSessions.length > 0 ? (
          filteredSessions.map((session) => (
            <div
              key={session.id}
              className="glass-panel p-5 rounded-2xl border border-slate-800 flex flex-col md:flex-row md:items-center justify-between gap-4 hover:border-slate-700 transition-all"
            >
              <div className="space-y-1.5">
                <div className="flex items-center gap-3">
                  <h2 className="text-base font-bold text-white">
                    {session.title || `${session.interview_type.toUpperCase()} Interview`}
                  </h2>
                  <span
                    className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-md ${
                      session.status === 'completed'
                        ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                        : 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20'
                    }`}
                  >
                    {session.status.replace('_', ' ')}
                  </span>
                  <span className="text-[10px] text-slate-400 uppercase font-semibold bg-slate-800 px-2 py-0.5 rounded">
                    {session.mode}
                  </span>
                </div>

                <div className="flex items-center gap-4 text-xs text-slate-400">
                  <span className="flex items-center gap-1.5">
                    <Calendar className="w-3.5 h-3.5" />
                    {new Date(session.created_at).toLocaleDateString()}
                  </span>
                  <span>•</span>
                  <span>Target: {session.target_role || 'Engineering'}</span>
                  <span>•</span>
                  <span>{session.question_count} questions</span>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <Link
                  href={session.status === 'completed' ? `/history/${session.id}` : `/practice/${session.id}`}
                  className="flex items-center gap-1.5 text-xs font-semibold px-4 py-2 rounded-xl bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 transition-colors"
                >
                  <span>{session.status === 'completed' ? 'View Feedback' : 'Resume Session'}</span>
                  <ArrowRight className="w-3.5 h-3.5" />
                </Link>

                <button
                  onClick={() => handleDelete(session.id)}
                  className="p-2 text-slate-500 hover:text-rose-400 rounded-lg hover:bg-slate-900 transition-colors"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))
        ) : (
          <div className="glass-panel p-8 rounded-2xl border border-slate-800 text-center space-y-2 text-slate-500">
            <AlertCircle className="w-8 h-8 mx-auto text-slate-600" />
            <p className="text-sm">No interview sessions found.</p>
            <Link href="/practice" className="text-xs text-indigo-400 hover:underline inline-block">
              Start your first practice interview →
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
