'use client';

import React, { useEffect, useState, use } from 'react';
import { useRouter } from 'next/navigation';
import {
  Bot,
  Mic,
  MicOff,
  Send,
  Sparkles,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
  ArrowRight,
  RotateCcw,
} from 'lucide-react';
import { api, InterviewSession, Question, Feedback } from '@/lib/api';

export default function PracticeRoomPage({ params }: { params: Promise<{ id: string }> }) {
  const resolvedParams = use(params);
  const sessionId = Number(resolvedParams.id);
  const router = useRouter();

  const [session, setSession] = useState<InterviewSession | null>(null);
  const [currentQuestion, setCurrentQuestion] = useState<Question | null>(null);
  const [history, setHistory] = useState<Array<{ question: Question; answer?: string }>>([]);
  const [answerInput, setAnswerInput] = useState('');
  const [isRecording, setIsRecording] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Load session & start if needed
  useEffect(() => {
    async function init() {
      try {
        const s = await api.getSession(sessionId);
        setSession(s);

        if (s.status === 'completed') {
          setIsComplete(true);
          const fb = await api.getFeedback(sessionId).catch(() => null);
          if (fb) setFeedback(fb);
          setLoading(false);
          return;
        }

        const existingQuestions = await api.getQuestions(sessionId);
        if (existingQuestions.length > 0) {
          setCurrentQuestion(existingQuestions[existingQuestions.length - 1]);
        } else {
          const firstQ = await api.startSession(sessionId);
          setCurrentQuestion(firstQ);
        }
      } catch (err: any) {
        setError(err.message || 'Failed to load practice room');
      } finally {
        setLoading(false);
      }
    }
    init();
  }, [sessionId]);

  // Voice recording using Web Speech API fallback
  const toggleRecording = () => {
    if (!('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)) {
      alert('Speech recognition is not natively supported in this browser. Please type your response.');
      return;
    }

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;

    if (!isRecording) {
      setIsRecording(true);
      recognition.onresult = (event: any) => {
        let transcript = '';
        for (let i = 0; i < event.results.length; i++) {
          transcript += event.results[i][0].transcript + ' ';
        }
        setAnswerInput(transcript.trim());
      };
      recognition.onerror = () => setIsRecording(false);
      recognition.onend = () => setIsRecording(false);
      recognition.start();
      (window as any)._currentRecognition = recognition;
    } else {
      setIsRecording(false);
      if ((window as any)._currentRecognition) {
        (window as any)._currentRecognition.stop();
      }
    }
  };

  const handleSubmitAnswer = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!answerInput.trim() || !currentQuestion || submitting) return;

    setSubmitting(true);
    setError(null);

    try {
      const res = await api.submitAnswer(
        sessionId,
        currentQuestion.id,
        answerInput.trim(),
        isRecording ? 'voice' : 'text'
      );

      // Save to local Q&A history
      setHistory((prev) => [...prev, { question: currentQuestion, answer: answerInput.trim() }]);
      setAnswerInput('');
      if (isRecording) toggleRecording();

      if (res.is_complete || !res.next_question) {
        // Complete interview and generate feedback
        setIsComplete(true);
        const completeRes = await api.completeSession(sessionId);
        if (completeRes.feedback) {
          const fb = await api.getFeedback(sessionId).catch(() => null);
          setFeedback(fb);
        }
      } else {
        setCurrentQuestion(res.next_question);
      }
    } catch (err: any) {
      setError(err.message || 'Error submitting response');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-slate-400">Initializing practice room...</p>
        </div>
      </div>
    );
  }

  // Completion / Scorecard View
  if (isComplete) {
    return (
      <div className="max-w-4xl mx-auto space-y-8 animate-in fade-in duration-300">
        <div className="glass-panel p-8 rounded-2xl border border-slate-800 text-center space-y-4">
          <div className="w-16 h-16 rounded-2xl bg-emerald-500/10 text-emerald-400 flex items-center justify-center mx-auto border border-emerald-500/20">
            <CheckCircle2 className="w-8 h-8" />
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Interview Round Completed!</h1>
          <p className="text-sm text-slate-400 max-w-md mx-auto">
            Your performance has been evaluated across technical accuracy, STAR framing, and communication.
          </p>

          {feedback && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-6">
              <div className="p-4 rounded-xl bg-slate-900/80 border border-slate-800">
                <p className="text-xs text-slate-400 font-medium">Overall Score</p>
                <p className="text-2xl font-bold text-amber-400 mt-1">{feedback.overall_score || 0} / 10</p>
              </div>
              <div className="p-4 rounded-xl bg-slate-900/80 border border-slate-800">
                <p className="text-xs text-slate-400 font-medium">Technical</p>
                <p className="text-2xl font-bold text-emerald-400 mt-1">{feedback.technical_score || 0} / 10</p>
              </div>
              <div className="p-4 rounded-xl bg-slate-900/80 border border-slate-800">
                <p className="text-xs text-slate-400 font-medium">Communication</p>
                <p className="text-2xl font-bold text-purple-400 mt-1">{feedback.communication_score || 0} / 10</p>
              </div>
              <div className="p-4 rounded-xl bg-slate-900/80 border border-slate-800">
                <p className="text-xs text-slate-400 font-medium">Confidence</p>
                <p className="text-2xl font-bold text-blue-400 mt-1">{feedback.confidence_score || 0} / 10</p>
              </div>
            </div>
          )}

          {feedback?.improvement_plan && (
            <div className="p-4 rounded-xl bg-indigo-500/10 border border-indigo-500/20 text-left mt-6">
              <p className="text-xs font-bold text-indigo-400 uppercase tracking-wider mb-1">Improvement Roadmap</p>
              <p className="text-sm text-slate-200">{feedback.improvement_plan}</p>
            </div>
          )}

          <div className="flex justify-center gap-4 pt-6">
            <button
              onClick={() => router.push('/practice')}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold transition-all"
            >
              <RotateCcw className="w-4 h-4" />
              <span>Practice Another Round</span>
            </button>
            <button
              onClick={() => router.push(`/history/${sessionId}`)}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm font-semibold border border-slate-700 transition-all"
            >
              <span>View Full Feedback Details</span>
              <ArrowRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6 animate-in fade-in duration-300">
      {/* Top Session Status Bar */}
      <div className="flex items-center justify-between px-4 py-3 rounded-xl bg-slate-900/70 border border-slate-800">
        <div className="flex items-center gap-3">
          <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse" />
          <div>
            <p className="text-xs font-bold text-white uppercase tracking-wider">
              {session?.interview_type} Interview Simulation
            </p>
            <p className="text-[11px] text-slate-400">Target Role: {session?.target_role}</p>
          </div>
        </div>

        <div className="text-xs font-semibold text-slate-300 bg-slate-800 px-3 py-1 rounded-md">
          Question {(currentQuestion?.order_index ?? 0) + 1} of {session?.question_count}
        </div>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
          {error}
        </div>
      )}

      {/* Current AI Question Box */}
      <div className="glass-panel-glow p-6 md:p-8 rounded-2xl space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs font-semibold text-indigo-400">
            <Bot className="w-4 h-4" />
            <span>AI Interviewer</span>
            {currentQuestion?.is_follow_up && (
              <span className="bg-purple-500/20 text-purple-300 px-2 py-0.5 rounded text-[10px] uppercase">
                Adaptive Follow-up
              </span>
            )}
          </div>
        </div>

        <p className="text-lg md:text-xl font-medium text-white leading-relaxed">
          {currentQuestion?.content || 'Generating question...'}
        </p>

        {currentQuestion?.ai_classification?.suggested_approach && (
          <div className="p-3 rounded-xl bg-slate-900/80 border border-slate-800 text-xs text-slate-400 flex items-start gap-2.5">
            <HelpCircle className="w-4 h-4 text-indigo-400 flex-shrink-0 mt-0.5" />
            <div>
              <span className="font-semibold text-slate-300">Coaching Hint: </span>
              {currentQuestion.ai_classification.suggested_approach}
            </div>
          </div>
        )}
      </div>

      {/* Candidate Response Area */}
      <form onSubmit={handleSubmitAnswer} className="space-y-4">
        <div className="relative">
          <textarea
            rows={5}
            value={answerInput}
            onChange={(e) => setAnswerInput(e.target.value)}
            placeholder="Type your response or use microphone to speak naturally (STAR format recommended for behavioral questions)..."
            className="w-full p-4 rounded-2xl bg-slate-900/80 border border-slate-800 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-all resize-none"
          />

          <div className="absolute bottom-4 right-4 flex items-center gap-2">
            <button
              type="button"
              onClick={toggleRecording}
              className={`p-2.5 rounded-xl border transition-all ${
                isRecording
                  ? 'bg-rose-500/20 border-rose-500 text-rose-400 animate-pulse'
                  : 'bg-slate-800 border-slate-700 text-slate-300 hover:text-white'
              }`}
              title={isRecording ? 'Stop Recording' : 'Record Voice'}
            >
              {isRecording ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
            </button>
          </div>
        </div>

        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-500">
            {answerInput.split(/\s+/).filter(Boolean).length} words
          </span>

          <button
            type="submit"
            disabled={!answerInput.trim() || submitting}
            className="flex items-center gap-2 px-6 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold transition-all shadow-md shadow-indigo-600/25 disabled:opacity-50"
          >
            {submitting ? (
              <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
            <span>Submit Answer</span>
          </button>
        </div>
      </form>
    </div>
  );
}
