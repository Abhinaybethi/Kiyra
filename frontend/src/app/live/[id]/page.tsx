'use client';

import React, { useEffect, useState, useRef, use } from 'react';
import { useRouter } from 'next/navigation';
import {
  Radio,
  Mic,
  MicOff,
  Volume2,
  VolumeX,
  Sparkles,
  Bot,
  Send,
  Zap,
  CheckCircle2,
  HelpCircle,
  Briefcase,
  Layers,
  ChevronRight,
  ShieldCheck,
  RotateCcw,
  Eye,
  Copy,
  Check,
  ExternalLink,
  Sliders,
  Monitor,
} from 'lucide-react';
import { api, WS_BASE, InterviewSession, Profile } from '@/lib/api';

interface TranscriptItem {
  id: string;
  speaker: 'interviewer' | 'candidate' | 'unknown';
  text: string;
  timestamp: string;
  isQuestion?: boolean;
}

interface AnswerData {
  answer?: string;
  key_points?: string[];
  star?: {
    situation?: string;
    task?: string;
    action?: string;
    result?: string;
  };
  follow_up_questions?: string[];
  confidence?: number;
  missing_context?: string;
}

export default function LiveWorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const resolvedParams = use(params);
  const sessionId = Number(resolvedParams.id);
  const router = useRouter();

  const [session, setSession] = useState<InterviewSession | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [transcripts, setTranscripts] = useState<TranscriptItem[]>([]);
  const [detectedQuestion, setDetectedQuestion] = useState<string>('');
  const [currentAnswer, setCurrentAnswer] = useState<AnswerData | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [isSystemAudioActive, setIsSystemAudioActive] = useState(false);
  const [autoAnswer, setAutoAnswer] = useState(true);
  const [isTeleprompter, setIsTeleprompter] = useState(false);
  const [showStealthModal, setShowStealthModal] = useState(false);
  const [manualQuery, setManualQuery] = useState('');
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const recognitionRef = useRef<any>(null);
  const displayStreamRef = useRef<MediaStream | null>(null);

  useEffect(() => {
    // 1. Fetch Session & Profile
    api.getSession(sessionId).then(setSession).catch(() => {});
    api.getProfile().then(setProfile).catch(() => {});

    // 2. Connect WebSocket
    const wsUrl = `${WS_BASE}/api/interviews/${sessionId}/ws`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setWsConnected(true);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        const { type, payload } = msg;

        if (type === 'transcript.final' || type === 'transcript.partial') {
          if (payload.text) {
            setTranscripts((prev) => [
              ...prev,
              {
                id: Math.random().toString(),
                speaker: payload.speaker || 'interviewer',
                text: payload.text,
                timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
              },
            ]);
          }
        } else if (type === 'question.detected') {
          setDetectedQuestion(payload.question);
          if (autoAnswer && payload.question) {
            handleManualTrigger(undefined, payload.question);
          }
        } else if (type === 'answer.generating') {
          setIsGenerating(true);
        } else if (type === 'answer.generated') {
          setIsGenerating(false);
          setCurrentAnswer(payload);
        } else if (type === 'session.error') {
          console.warn('WS Session warning:', payload.message);
        }
      } catch (err) {
        console.error('WS Parse Error:', err);
      }
    };

    ws.onclose = () => setWsConnected(false);

    return () => {
      if (ws.readyState === WebSocket.OPEN) ws.close();
      if (recognitionRef.current) recognitionRef.current.stop();
      if (displayStreamRef.current) {
        displayStreamRef.current.getTracks().forEach((t) => t.stop());
      }
    };
  }, [sessionId, autoAnswer]);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcripts]);

  // System Audio Loopback Capture (Zoom / Google Meet / Teams Audio)
  const toggleSystemAudio = async () => {
    if (isSystemAudioActive) {
      if (displayStreamRef.current) {
        displayStreamRef.current.getTracks().forEach((t) => t.stop());
        displayStreamRef.current = null;
      }
      setIsSystemAudioActive(false);
      return;
    }

    try {
      // Capture Tab / System audio via getDisplayMedia
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        } as any,
      });

      const audioTrack = stream.getAudioTracks()[0];
      if (!audioTrack) {
        alert('No audio track selected! Please make sure to check "Share audio" when choosing the meeting tab or screen.');
        stream.getTracks().forEach((t) => t.stop());
        return;
      }

      displayStreamRef.current = stream;
      setIsSystemAudioActive(true);

      // Start continuous speech recognition simultaneously
      if (!isRecording) {
        toggleLiveMic();
      }

      audioTrack.onended = () => {
        setIsSystemAudioActive(false);
      };
    } catch (err: any) {
      if (err.name !== 'NotAllowedError') {
        setError('Failed to capture system audio: ' + err.message);
      }
    }
  };

  // Audio recording & speech recognition loop
  const toggleLiveMic = () => {
    if (!('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)) {
      alert('Speech Recognition is not supported in this browser. You can use the manual query box or Desktop Stealth App.');
      return;
    }

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

    if (!isRecording) {
      const recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = false;
      recognition.lang = 'en-US';

      recognition.onresult = (event: any) => {
        const lastResult = event.results[event.results.length - 1];
        if (lastResult.isFinal) {
          const text = lastResult[0].transcript.trim();
          if (text) {
            if (wsRef.current?.readyState === WebSocket.OPEN) {
              wsRef.current.send(
                JSON.stringify({
                  type: 'transcript.text',
                  payload: { text, speaker: 'interviewer', force_answer: autoAnswer },
                })
              );
            }
          }
        }
      };

      recognition.onerror = () => setIsRecording(false);
      recognition.onend = () => {
        if (isRecording) {
          try {
            recognition.start();
          } catch {}
        }
      };

      recognition.start();
      recognitionRef.current = recognition;
      setIsRecording(true);
    } else {
      setIsRecording(false);
      if (recognitionRef.current) {
        recognitionRef.current.stop();
        recognitionRef.current = null;
      }
    }
  };

  // Manual Trigger Answer
  const handleManualTrigger = async (e?: React.FormEvent, overrideQuery?: string) => {
    if (e) e.preventDefault();
    const query = overrideQuery || manualQuery.trim() || detectedQuestion;
    if (!query) return;

    setIsGenerating(true);
    setDetectedQuestion(query);

    try {
      const result = await api.suggestAnswer(query, 'unknown', sessionId);
      setCurrentAnswer(result);
      setManualQuery('');
    } catch (err: any) {
      setError(err.message || 'Failed to generate answer');
    } finally {
      setIsGenerating(false);
    }
  };

  return (
    <div className="h-[calc(100vh-6.5rem)] flex flex-col space-y-4 animate-in fade-in duration-300">
      {/* Top Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 rounded-2xl bg-slate-900/90 border border-slate-800 flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span
              className={`w-3 h-3 rounded-full ${
                wsConnected ? 'bg-emerald-400 animate-pulse' : 'bg-rose-500'
              }`}
            />
            <span className="text-xs font-bold text-white uppercase tracking-wider">
              {session?.title || 'Live Assistant Workspace'}
            </span>
          </div>
          <span className="text-slate-600">|</span>
          <span className="text-xs text-slate-400">
            Target: <span className="text-slate-200 font-medium">{session?.target_role || 'Engineering'}</span>
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          {/* Desktop Stealth Overlay Guide Button */}
          <button
            onClick={() => setShowStealthModal(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-indigo-950/60 border border-indigo-500/30 text-indigo-300 hover:bg-indigo-900/60 transition-all"
            title="Launch OS Screen-Share Invisible Desktop Window"
          >
            <ShieldCheck className="w-3.5 h-3.5 text-indigo-400" />
            <span>Stealth Desktop App</span>
          </button>

          {/* Teleprompter Toggle */}
          <button
            onClick={() => setIsTeleprompter(!isTeleprompter)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border transition-all ${
              isTeleprompter
                ? 'bg-purple-600 border-purple-500 text-white shadow-lg shadow-purple-500/20'
                : 'bg-slate-800/80 border-slate-700 text-slate-300 hover:text-white'
            }`}
          >
            <Eye className="w-3.5 h-3.5" />
            <span>{isTeleprompter ? 'Normal View' : 'Teleprompter HUD'}</span>
          </button>

          {/* Auto-Pilot Toggle */}
          <button
            onClick={() => setAutoAnswer(!autoAnswer)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border transition-all ${
              autoAnswer
                ? 'bg-emerald-500/20 border-emerald-500 text-emerald-300'
                : 'bg-slate-800/80 border-slate-700 text-slate-400 hover:text-slate-200'
            }`}
          >
            <Zap className={`w-3.5 h-3.5 ${autoAnswer ? 'text-emerald-400 fill-emerald-400' : ''}`} />
            <span>Auto-Answer {autoAnswer ? 'ON' : 'OFF'}</span>
          </button>

          {/* System Audio Loopback (Interviewer Audio from Zoom/Meet) */}
          <button
            onClick={toggleSystemAudio}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border transition-all ${
              isSystemAudioActive
                ? 'bg-cyan-500/20 border-cyan-500 text-cyan-300 animate-pulse'
                : 'bg-slate-800/80 border-slate-700 text-slate-300 hover:text-white'
            }`}
            title="Capture digital audio directly from Zoom / Google Meet / Teams tab"
          >
            {isSystemAudioActive ? <Volume2 className="w-3.5 h-3.5" /> : <Volume2 className="w-3.5 h-3.5 text-cyan-400" />}
            <span>{isSystemAudioActive ? 'System Audio ON' : 'Listen Meeting Audio'}</span>
          </button>

          {/* Microphone Toggle */}
          <button
            onClick={toggleLiveMic}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl text-xs font-semibold border transition-all ${
              isRecording
                ? 'bg-rose-500/20 border-rose-500 text-rose-300 animate-pulse'
                : 'bg-slate-800 border-slate-700 text-slate-300 hover:text-white'
            }`}
          >
            {isRecording ? <MicOff className="w-3.5 h-3.5" /> : <Mic className="w-3.5 h-3.5 text-rose-400" />}
            <span>{isRecording ? 'Mic Active' : 'Listen Mic'}</span>
          </button>

          <button
            onClick={() => router.push('/history')}
            className="text-xs text-slate-400 hover:text-slate-200 px-3 py-1.5 rounded-lg bg-slate-800/60 border border-slate-700/60"
          >
            End Assist
          </button>
        </div>
      </div>

      {/* Main Content: Teleprompter Mode OR 3-Panel Workspace */}
      {isTeleprompter ? (
        /* ── TELEPROMPTER VIEW ── */
        <div className="flex-1 glass-panel p-6 rounded-3xl border border-indigo-500/30 flex flex-col space-y-4 overflow-hidden bg-slate-950/90 shadow-2xl">
          <div className="flex items-center justify-between border-b border-slate-800 pb-3">
            <div className="flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse" />
              <h2 className="text-sm font-bold text-indigo-300 uppercase tracking-wider">
                Stealth Teleprompter View (Dock near webcam)
              </h2>
            </div>
            <div className="flex items-center gap-3 text-xs text-slate-400">
              <span>Auto-Pilot: <strong className="text-emerald-400">{autoAnswer ? 'ACTIVE' : 'MANUAL'}</strong></span>
              <span>•</span>
              <span>Audio: <strong className={isRecording || isSystemAudioActive ? 'text-cyan-400' : 'text-slate-500'}>{isRecording || isSystemAudioActive ? 'STREAMING' : 'IDLE'}</strong></span>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto space-y-4 pr-2">
            {detectedQuestion && (
              <div className="p-4 rounded-2xl bg-indigo-950/40 border border-indigo-500/30">
                <p className="text-[10px] font-bold text-indigo-400 uppercase tracking-wider mb-1">Detected Question</p>
                <p className="text-base font-bold text-white leading-snug">{detectedQuestion}</p>
              </div>
            )}

            {currentAnswer ? (
              <div className="space-y-4">
                {currentAnswer.answer && (
                  <div className="p-5 rounded-2xl bg-slate-900/90 border border-slate-700/80 shadow-lg space-y-2">
                    <p className="text-xs font-bold text-emerald-400 uppercase tracking-wider flex items-center gap-1.5">
                      <Sparkles className="w-3.5 h-3.5" />
                      <span>Direct 30-Sec Soundbite (Speak this directly)</span>
                    </p>
                    <p className="text-sm text-slate-100 font-medium leading-relaxed">
                      {currentAnswer.answer}
                    </p>
                  </div>
                )}

                {currentAnswer.key_points && currentAnswer.key_points.length > 0 && (
                  <div className="p-4 rounded-2xl bg-slate-900/70 border border-slate-800 space-y-2">
                    <p className="text-xs font-bold text-indigo-300 uppercase tracking-wider">
                      Key Technical Talking Points
                    </p>
                    <ul className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs text-slate-200">
                      {currentAnswer.key_points.map((pt, i) => (
                        <li key={i} className="flex items-start gap-2">
                          <CheckCircle2 className="w-3.5 h-3.5 text-indigo-400 mt-0.5 flex-shrink-0" />
                          <span>{pt}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {currentAnswer.star && (
                  <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                    {currentAnswer.star.situation && (
                      <div className="p-3 rounded-xl bg-slate-900/60 border border-slate-800">
                        <p className="text-[10px] font-bold text-slate-400 uppercase">Situation</p>
                        <p className="text-xs text-slate-200 mt-1">{currentAnswer.star.situation}</p>
                      </div>
                    )}
                    {currentAnswer.star.task && (
                      <div className="p-3 rounded-xl bg-slate-900/60 border border-slate-800">
                        <p className="text-[10px] font-bold text-slate-400 uppercase">Task</p>
                        <p className="text-xs text-slate-200 mt-1">{currentAnswer.star.task}</p>
                      </div>
                    )}
                    {currentAnswer.star.action && (
                      <div className="p-3 rounded-xl bg-slate-900/60 border border-slate-800">
                        <p className="text-[10px] font-bold text-slate-400 uppercase">Action</p>
                        <p className="text-xs text-slate-200 mt-1">{currentAnswer.star.action}</p>
                      </div>
                    )}
                    {currentAnswer.star.result && (
                      <div className="p-3 rounded-xl bg-emerald-950/30 border border-emerald-500/30">
                        <p className="text-[10px] font-bold text-emerald-400 uppercase">Result</p>
                        <p className="text-xs text-emerald-200 mt-1">{currentAnswer.star.result}</p>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="h-48 flex flex-col items-center justify-center text-center text-slate-500 space-y-2">
                <Bot className="w-8 h-8 text-slate-600 animate-pulse" />
                <p className="text-sm text-slate-400">Teleprompter is ready. Listening to interviewer voice...</p>
              </div>
            )}
          </div>
        </div>
      ) : (
        /* ── 3-PANEL DEFAULT WORKSPACE ── */
        <div className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-4 min-h-0">
          {/* LEFT PANEL: Live Transcription Stream (4 cols) */}
          <div className="lg:col-span-4 glass-panel rounded-2xl border border-slate-800 flex flex-col min-h-0 overflow-hidden">
            <div className="px-4 py-3 border-b border-slate-800 flex items-center justify-between flex-shrink-0">
              <h2 className="text-xs font-bold text-slate-300 uppercase tracking-wider flex items-center gap-2">
                <Radio className="w-3.5 h-3.5 text-rose-400" />
                <span>Live Transcript</span>
              </h2>
              {(isRecording || isSystemAudioActive) && (
                <span className="text-[10px] bg-rose-500/20 text-rose-400 px-2 py-0.5 rounded-full font-bold">
                  STREAMING
                </span>
              )}
            </div>

            <div className="flex-1 p-4 overflow-y-auto space-y-3">
              {transcripts.length > 0 ? (
                transcripts.map((t) => (
                  <div
                    key={t.id}
                    className="p-3 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1 hover:border-slate-700 transition-colors"
                  >
                    <div className="flex items-center justify-between text-[10px] text-slate-500 font-medium">
                      <span className="text-indigo-400 font-semibold uppercase">{t.speaker}</span>
                      <span>{t.timestamp}</span>
                    </div>
                    <p className="text-xs text-slate-200 leading-relaxed">{t.text}</p>
                  </div>
                ))
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-center p-6 text-slate-500 space-y-2">
                  <Mic className="w-8 h-8 text-slate-600" />
                  <p className="text-xs">Audio listeners are idle.</p>
                  <p className="text-[11px] text-slate-600">Click &ldquo;Listen Meeting Audio&rdquo; or &ldquo;Listen Mic&rdquo; above.</p>
                </div>
              )}
              <div ref={transcriptEndRef} />
            </div>

            {/* Quick Manual Inject */}
            <form onSubmit={handleManualTrigger} className="p-3 border-t border-slate-800 flex-shrink-0 bg-slate-950/40">
              <div className="relative">
                <input
                  type="text"
                  value={manualQuery}
                  onChange={(e) => setManualQuery(e.target.value)}
                  placeholder="Manual trigger: type interviewer question..."
                  className="w-full pl-3 pr-10 py-2 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                />
                <button
                  type="submit"
                  className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-lg text-indigo-400 hover:text-white"
                >
                  <Send className="w-3.5 h-3.5" />
                </button>
              </div>
            </form>
          </div>

          {/* CENTER PANEL: AI Live Assistance & STAR Suggestions (5 cols) */}
          <div className="lg:col-span-5 glass-panel-glow rounded-2xl flex flex-col min-h-0 overflow-hidden">
            <div className="px-5 py-3 border-b border-indigo-500/20 bg-indigo-950/20 flex items-center justify-between flex-shrink-0">
              <h2 className="text-xs font-bold text-indigo-300 uppercase tracking-wider flex items-center gap-2">
                <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
                <span>Real-Time Answer Recommendations</span>
              </h2>
              {isGenerating && (
                <div className="flex items-center gap-1.5 text-xs text-indigo-400">
                  <span className="w-2 h-2 rounded-full bg-indigo-400 animate-ping" />
                  <span>Synthesizing...</span>
                </div>
              )}
            </div>

            <div className="flex-1 p-5 overflow-y-auto space-y-4">
              {detectedQuestion && (
                <div className="p-4 rounded-xl bg-indigo-500/10 border border-indigo-500/20 space-y-1">
                  <p className="text-[10px] font-bold text-indigo-400 uppercase tracking-wider">
                    Detected Question
                  </p>
                  <p className="text-sm font-semibold text-white">{detectedQuestion}</p>
                </div>
              )}

              {currentAnswer ? (
                <div className="space-y-4">
                  {/* Direct Soundbite */}
                  {currentAnswer.answer && (
                    <div className="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 space-y-1.5">
                      <p className="text-[11px] font-bold text-emerald-400 uppercase tracking-wider flex items-center gap-1">
                        <Sparkles className="w-3 h-3" />
                        <span>Direct 30-Sec Soundbite</span>
                      </p>
                      <p className="text-xs text-slate-100 font-medium leading-relaxed">
                        {currentAnswer.answer}
                      </p>
                    </div>
                  )}

                  {/* Key Points */}
                  {currentAnswer.key_points && currentAnswer.key_points.length > 0 && (
                    <div className="space-y-2">
                      <p className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">
                        Key Talking Points
                      </p>
                      <div className="space-y-1.5">
                        {currentAnswer.key_points.map((kp, idx) => (
                          <div key={idx} className="flex items-start gap-2 text-xs text-slate-200">
                            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 mt-0.5 flex-shrink-0" />
                            <span>{kp}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* STAR Breakdown */}
                  {currentAnswer.star && (
                    <div className="p-4 rounded-xl bg-slate-900/80 border border-slate-800 space-y-2.5">
                      <p className="text-[11px] font-bold text-indigo-400 uppercase tracking-wider flex items-center gap-1.5">
                        <Layers className="w-3.5 h-3.5" />
                        <span>STAR Answer Framework</span>
                      </p>
                      <div className="grid grid-cols-1 gap-2 text-xs">
                        {currentAnswer.star.situation && (
                          <div>
                            <span className="font-bold text-slate-400">Situation: </span>
                            <span className="text-slate-200">{currentAnswer.star.situation}</span>
                          </div>
                        )}
                        {currentAnswer.star.task && (
                          <div>
                            <span className="font-bold text-slate-400">Task: </span>
                            <span className="text-slate-200">{currentAnswer.star.task}</span>
                          </div>
                        )}
                        {currentAnswer.star.action && (
                          <div>
                            <span className="font-bold text-slate-400">Action: </span>
                            <span className="text-slate-200">{currentAnswer.star.action}</span>
                          </div>
                        )}
                        {currentAnswer.star.result && (
                          <div>
                            <span className="font-bold text-emerald-400">Result: </span>
                            <span className="text-slate-200">{currentAnswer.star.result}</span>
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {/* Anticipated Follow-Ups */}
                  {currentAnswer.follow_up_questions && currentAnswer.follow_up_questions.length > 0 && (
                    <div className="p-3 rounded-xl bg-purple-500/10 border border-purple-500/20 text-xs space-y-1.5">
                      <p className="font-bold text-purple-400 uppercase text-[10px] tracking-wider">
                        Anticipated Follow-up Questions
                      </p>
                      {currentAnswer.follow_up_questions.map((fq, idx) => (
                        <p key={idx} className="text-slate-300">
                          • {fq}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-center p-6 text-slate-500 space-y-2">
                  <Bot className="w-8 h-8 text-slate-600" />
                  <p className="text-xs">Awaiting detected question.</p>
                  <p className="text-[11px] text-slate-600">
                    AI will assemble STAR framing and real candidate context when the interviewer speaks.
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* RIGHT PANEL: Candidate Context & Quick Controls (3 cols) */}
          <div className="lg:col-span-3 glass-panel rounded-2xl border border-slate-800 flex flex-col min-h-0 overflow-hidden">
            <div className="px-4 py-3 border-b border-slate-800 flex-shrink-0">
              <h2 className="text-xs font-bold text-slate-300 uppercase tracking-wider flex items-center gap-2">
                <Briefcase className="w-3.5 h-3.5 text-indigo-400" />
                <span>Candidate Context</span>
              </h2>
            </div>

            <div className="flex-1 p-4 overflow-y-auto space-y-4 text-xs">
              <div className="p-3 rounded-xl bg-slate-900/60 border border-slate-800 space-y-1">
                <p className="text-[11px] text-slate-400 font-medium">Candidate</p>
                <p className="font-semibold text-white">{profile?.name || 'Local Candidate'}</p>
                <p className="text-[11px] text-slate-400">{profile?.target_role}</p>
              </div>

              {profile?.preferred_technologies && (
                <div className="space-y-1.5">
                  <p className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Skills In RAG</p>
                  <div className="flex flex-wrap gap-1.5">
                    {JSON.parse(profile.preferred_technologies).map((tech: string, i: number) => (
                      <span key={i} className="px-2 py-0.5 rounded-md bg-slate-800 border border-slate-700 text-[10px] text-slate-300">
                        {tech}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="p-3 rounded-xl bg-indigo-500/10 border border-indigo-500/20 text-[11px] text-indigo-300 space-y-1">
                <div className="flex items-center gap-1.5 font-bold">
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>OS Screen Protection</span>
                </div>
                <p className="text-slate-300 text-[10px] leading-relaxed">
                  For 100% invisible overlay during Zoom/Teams screen sharing, use the Python Desktop Stealth HUD.
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── STEALTH APP MODAL ── */}
      {showStealthModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4 animate-in fade-in">
          <div className="max-w-lg w-full glass-panel p-6 rounded-3xl border border-indigo-500/40 bg-slate-900/95 space-y-5 shadow-2xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <ShieldCheck className="w-5 h-5 text-indigo-400" />
                <h3 className="text-base font-bold text-white">Windows OS-Level Stealth HUD</h3>
              </div>
              <button
                onClick={() => setShowStealthModal(false)}
                className="text-slate-400 hover:text-white p-1 rounded-lg"
              >
                ✕
              </button>
            </div>

            <div className="space-y-3 text-xs text-slate-300 leading-relaxed">
              <p>
                The native Python Desktop HUD uses the Windows OS API <code className="text-indigo-300 bg-slate-800 px-1.5 py-0.5 rounded">SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)</code>.
              </p>
              <div className="p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 space-y-1">
                <p className="font-bold">🛡️ Guaranteed Invisibility:</p>
                <p className="text-[11px] text-slate-200">
                  Invisible in Zoom, Google Meet, MS Teams, OBS Studio screen captures, and screenshots, while remaining 100% visible on your monitor.
                </p>
              </div>

              <div className="space-y-1.5">
                <p className="font-semibold text-slate-200">How to Launch:</p>
                <div className="p-3 rounded-xl bg-slate-950 border border-slate-800 font-mono text-[11px] text-indigo-300 flex items-center justify-between">
                  <span>python desktop/stealth_overlay.py</span>
                  <button
                    onClick={() => navigator.clipboard.writeText('python desktop/stealth_overlay.py')}
                    className="text-slate-400 hover:text-white"
                    title="Copy command"
                  >
                    <Copy className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              <div className="space-y-1 text-slate-400 text-[11px]">
                <p><strong className="text-slate-200">Hotkeys:</strong></p>
                <p>• <code className="text-slate-200 font-mono">F9</code>: Emergency Panic Hide / Show</p>
                <p>• <code className="text-slate-200 font-mono">F10</code>: Click-Through Mode (pass clicks to IDE/Meeting)</p>
                <p>• <code className="text-slate-200 font-mono">F8</code>: Force Generate Answer</p>
                <p>• <code className="text-slate-200 font-mono">F7</code>: Clear Content</p>
              </div>
            </div>

            <div className="flex justify-end">
              <button
                onClick={() => setShowStealthModal(false)}
                className="px-4 py-2 rounded-xl bg-indigo-600 text-xs font-semibold text-white hover:bg-indigo-500"
              >
                Got it
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
