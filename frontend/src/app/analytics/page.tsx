'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  BarChart3,
  TrendingUp,
  Award,
  CheckCircle2,
  Calendar,
  Sparkles,
  Bot,
  AlertCircle,
  ArrowRight,
} from 'lucide-react';
import { api, DashboardData } from '@/lib/api';

export default function AnalyticsPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getDashboard().then(setData).finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-slate-400">Loading interview performance analytics...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="space-y-1">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-xs font-semibold text-indigo-400">
          <BarChart3 className="w-3.5 h-3.5" />
          <span>Performance & Competency Progression</span>
        </div>
        <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Interview Analytics</h1>
        <p className="text-sm text-slate-400">
          Computed from real session feedback and coach evaluations. Note: AI behavioral scores are estimates, not psychological truth.
        </p>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <p className="text-xs text-slate-400 font-medium">Completed Interviews</p>
          <p className="text-2xl font-bold text-white mt-1">
            {data?.completed_interviews || 0}
            <span className="text-xs text-slate-500 font-normal ml-1.5">/ {data?.total_interviews || 0} total</span>
          </p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <p className="text-xs text-slate-400 font-medium">Avg Overall Score</p>
          <p className="text-2xl font-bold text-amber-400 mt-1">
            {data?.avg_overall_score !== null && data?.avg_overall_score !== undefined ? `${data.avg_overall_score} / 10` : '—'}
          </p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <p className="text-xs text-slate-400 font-medium">Technical Score</p>
          <p className="text-2xl font-bold text-emerald-400 mt-1">
            {data?.avg_technical_score !== null && data?.avg_technical_score !== undefined ? `${data.avg_technical_score} / 10` : '—'}
          </p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <p className="text-xs text-slate-400 font-medium">Communication Score</p>
          <p className="text-2xl font-bold text-purple-400 mt-1">
            {data?.avg_communication_score !== null && data?.avg_communication_score !== undefined ? `${data.avg_communication_score} / 10` : '—'}
          </p>
        </div>
      </div>

      {/* Performance Score Progression Table */}
      <div className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-4">
        <h2 className="text-base font-bold text-white flex items-center gap-2">
          <TrendingUp className="w-4 h-4 text-emerald-400" />
          <span>Score Progression by Completed Session</span>
        </h2>

        {data?.score_trend && data.score_trend.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400 font-bold uppercase tracking-wider">
                  <th className="pb-3">Session Date</th>
                  <th className="pb-3">Domain Type</th>
                  <th className="pb-3">Score</th>
                  <th className="pb-3 text-right">Performance Band</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {data.score_trend.map((st, i) => (
                  <tr key={i} className="hover:bg-slate-900/40 transition-colors">
                    <td className="py-3 text-slate-300">
                      {new Date(st.date).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}
                    </td>
                    <td className="py-3 uppercase font-semibold text-indigo-400">{st.type}</td>
                    <td className="py-3 font-bold text-white">{st.score !== null && st.score !== undefined ? `${st.score} / 10` : '—'}</td>
                    <td className="py-3 text-right">
                      {st.score && st.score >= 8.5 ? (
                        <span className="text-emerald-400 font-semibold">Strong Hire</span>
                      ) : st.score && st.score >= 7.0 ? (
                        <span className="text-indigo-400 font-semibold">Hire</span>
                      ) : (
                        <span className="text-amber-400 font-semibold">Needs Preparation</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-8 text-slate-500 space-y-2">
            <AlertCircle className="w-8 h-8 mx-auto text-slate-600" />
            <p className="text-sm">No completed interview score records to graph yet.</p>
            <Link href="/practice" className="text-xs font-semibold text-indigo-400 hover:underline">
              Complete mock interviews to track your skill curve →
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
