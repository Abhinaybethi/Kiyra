'use client';

import React, { useEffect, useState, use } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import {
  ArrowLeft,
  Award,
  CheckCircle2,
  AlertTriangle,
  Lightbulb,
  Sparkles,
  Bot,
  User,
  Calendar,
  Layers,
} from 'lucide-react';
import { api, InterviewSession, Feedback, Question } from '@/lib/api';

export default function SessionFeedbackPage({ params }: { params: Promise<{ id: string }> }) {
  const resolvedParams = use(params);
  const sessionId = Number(resolvedParams.id);
  const router = useRouter();

  const [session, setSession] = useState<InterviewSession | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [sessionAnalytics, setSessionAnalytics] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      api.getSession(sessionId).then(setSession).catch(() => null),
      api.getFeedback(sessionId).then(setFeedback).catch(() => null),
      api.getQuestions(sessionId).then(setQuestions).catch(() => []),
      api.getSessionAnalytics(sessionId).then(setSessionAnalytics).catch(() => null),
    ]).finally(() => setLoading(false));
  }, [sessionId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-slate-400">Loading interview scorecard...</p>
        </div>
      </div>
    );
  }

  const strengthsList: string[] = feedback?.strengths ? JSON.parse(feedback.strengths) : [];
  const weaknessesList: string[] = feedback?.weaknesses ? JSON.parse(feedback.weaknesses) : [];
  const topicsList: string[] = feedback?.recommended_topics ? JSON.parse(feedback.recommended_topics) : [];

  return (
    <div className="max-w-5xl mx-auto space-y-8 animate-in fade-in duration-300">
      {/* Back Header */}
      <div className="flex items-center justify-between">
        <Link
          href="/history"
          className="flex items-center gap-2 text-xs font-semibold text-slate-400 hover:text-white transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          <span>Back to Session History</span>
        </Link>
        <span className="text-xs text-slate-500 font-mono">Session #{sessionId}</span>
      </div>

      {/* Hero Overview */}
      <div className="glass-panel p-6 md:p-8 rounded-2xl border border-slate-800 space-y-6">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-bold text-white tracking-tight">
                {session?.title || `${session?.interview_type.toUpperCase()} Interview Scorecard`}
              </h1>
            </div>
            <p className="text-xs text-slate-400">
              Role: <span className="text-slate-200 font-medium">{session?.target_role}</span> • Completed{' '}
              {session?.ended_at ? new Date(session.ended_at).toLocaleString() : 'Recently'}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <span className="px-3 py-1 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-bold uppercase">
              {session?.interview_type} Round
            </span>
          </div>
        </div>

        {/* Score Grid */}
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 pt-4 border-t border-slate-800/80">
          <div className="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800 text-center">
            <p className="text-[11px] text-slate-400 font-medium">Overall Score</p>
            <p className="text-2xl font-bold text-amber-400 mt-1">{feedback?.overall_score ?? '—'} / 10</p>
          </div>
          <div className="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800 text-center">
            <p className="text-[11px] text-slate-400 font-medium">Technical Depth</p>
            <p className="text-2xl font-bold text-emerald-400 mt-1">{feedback?.technical_score ?? '—'} / 10</p>
          </div>
          <div className="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800 text-center">
            <p className="text-[11px] text-slate-400 font-medium">Communication</p>
            <p className="text-2xl font-bold text-purple-400 mt-1">{feedback?.communication_score ?? '—'} / 10</p>
          </div>
          <div className="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800 text-center">
            <p className="text-[11px] text-slate-400 font-medium">Confidence</p>
            <p className="text-2xl font-bold text-blue-400 mt-1">{feedback?.confidence_score ?? '—'} / 10</p>
          </div>
          <div className="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800 text-center">
            <p className="text-[11px] text-slate-400 font-medium">Relevance</p>
            <p className="text-2xl font-bold text-rose-400 mt-1">{feedback?.relevance_score ?? '—'} / 10</p>
          </div>
        </div>
      </div>

      {/* Strengths & Improvement Plan */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Key Strengths */}
        <div className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-4">
          <h2 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-400" />
            <span>Demonstrated Strengths</span>
          </h2>
          {strengthsList.length > 0 ? (
            <ul className="space-y-2.5">
              {strengthsList.map((str, idx) => (
                <li key={idx} className="flex items-start gap-2.5 text-xs text-slate-200 bg-slate-900/60 p-3 rounded-xl border border-slate-800">
                  <span className="text-emerald-400 font-bold">•</span>
                  <span>{str}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-slate-500">No specific strengths isolated.</p>
          )}
        </div>

        {/* Growth Areas & Missed Opportunities */}
        <div className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-4">
          <h2 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-400" />
            <span>Growth Areas & Missed Points</span>
          </h2>
          {weaknessesList.length > 0 ? (
            <ul className="space-y-2.5">
              {weaknessesList.map((weak, idx) => (
                <li key={idx} className="flex items-start gap-2.5 text-xs text-slate-200 bg-slate-900/60 p-3 rounded-xl border border-slate-800">
                  <span className="text-amber-400 font-bold">•</span>
                  <span>{weak}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-slate-500">No major weaknesses identified.</p>
          )}

          {feedback?.missed_opportunities && (
            <div className="p-3.5 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs text-amber-300 space-y-1">
              <p className="font-bold">Missed Opportunity Highlight:</p>
              <p className="text-slate-300 leading-relaxed">{feedback.missed_opportunities}</p>
            </div>
          )}
        </div>
      </div>

      {/* Actionable Improvement Roadmap */}
      {feedback?.improvement_plan && (
        <div className="glass-panel p-6 rounded-2xl border border-indigo-500/30 bg-indigo-950/20 space-y-3">
          <h2 className="text-sm font-bold text-indigo-300 uppercase tracking-wider flex items-center gap-2">
            <Lightbulb className="w-4 h-4 text-indigo-400" />
            <span>Actionable Coach Improvement Plan</span>
          </h2>
          <p className="text-sm text-slate-200 leading-relaxed">{feedback.improvement_plan}</p>

          {topicsList.length > 0 && (
            <div className="pt-3 border-t border-indigo-500/20 flex items-center gap-2 flex-wrap">
              <span className="text-xs text-indigo-400 font-semibold">Recommended Topics to Study:</span>
              {topicsList.map((topic, i) => (
                <span key={i} className="px-2.5 py-0.5 rounded-lg bg-indigo-600/20 text-indigo-300 text-xs border border-indigo-500/30">
                  {topic}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Full Q&A Transcript Review */}
      <div className="space-y-4">
        <h2 className="text-base font-bold text-white flex items-center gap-2">
          <Layers className="w-4 h-4 text-slate-400" />
          <span>Full Q&A Transcript</span>
        </h2>

        {sessionAnalytics?.questions && sessionAnalytics.questions.length > 0 ? (
          sessionAnalytics.questions.map((qa: any, index: number) => (
            <div key={index} className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-3">
              <div className="flex items-center justify-between text-xs text-indigo-400 font-semibold">
                <span className="flex items-center gap-2">
                  <Bot className="w-4 h-4" />
                  <span>Question {index + 1} ({qa.question_type})</span>
                </span>
                <span className="text-slate-500">{qa.word_count || 0} words answered</span>
              </div>

              <p className="text-sm font-medium text-white">{qa.question}</p>

              <div className="p-3.5 rounded-xl bg-slate-900/60 border border-slate-800/80 text-xs text-slate-300 space-y-1">
                <div className="flex items-center gap-1.5 text-slate-400 font-bold text-[10px] uppercase">
                  <User className="w-3.5 h-3.5 text-slate-500" />
                  <span>Your Answer</span>
                </div>
                <p className="leading-relaxed whitespace-pre-line">{qa.answer || '[No response recorded]'}</p>
              </div>
            </div>
          ))
        ) : (
          <p className="text-xs text-slate-500">No question transcripts stored.</p>
        )}
      </div>
    </div>
  );
}
