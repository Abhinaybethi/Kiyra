'use client';

import React, { useEffect, useState } from 'react';
import {
  Settings,
  Cpu,
  ShieldCheck,
  CheckCircle2,
  AlertCircle,
  RefreshCw,
  Sliders,
  HardDrive,
} from 'lucide-react';
import { api, ModelConfig, Profile } from '@/lib/api';

export default function SettingsPage() {
  const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [ollamaAvailable, setOllamaAvailable] = useState<boolean>(true);
  const [provider, setProvider] = useState('ollama');
  const [modelName, setModelName] = useState('llama3.2:3b');
  const [embeddingModel, setEmbeddingModel] = useState('nomic-embed-text');
  const [transcriptionModel, setTranscriptionModel] = useState('base');
  const [candidateName, setCandidateName] = useState('');
  const [targetRole, setTargetRole] = useState('');
  const [saving, setSaving] = useState(false);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadData = () => {
    api.getModelConfig().then((cfg) => {
      setModelConfig(cfg);
      setProvider(cfg.provider);
      setModelName(cfg.model_name);
      setEmbeddingModel(cfg.embedding_model);
      setTranscriptionModel(cfg.transcription_model);
    }).catch(() => {});

    api.getProfile().then((p) => {
      if (p) {
        setProfile(p);
        setCandidateName(p.name);
        setTargetRole(p.target_role || '');
      }
    }).catch(() => {});

    api.getAvailableModels().then((res) => {
      setOllamaAvailable(res.ollama_available);
      setAvailableModels(res.models);
    }).catch(() => setOllamaAvailable(false));
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSuccessMsg(null);

    try {
      // 1. Update Model Config
      await api.updateModelConfig({
        provider,
        model_name: modelName,
        embedding_model: embeddingModel,
        transcription_model: transcriptionModel,
      });

      // 2. Update Profile
      if (candidateName) {
        await api.updateProfile({
          name: candidateName,
          target_role: targetRole,
        });
      }

      setSuccessMsg('Settings updated successfully!');
      loadData();
    } catch (err: any) {
      setError(err.message || 'Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-4xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="space-y-1">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-xs font-semibold text-indigo-400">
          <Settings className="w-3.5 h-3.5" />
          <span>System & Hardware Configuration</span>
        </div>
        <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Platform Settings</h1>
        <p className="text-sm text-slate-400">
          Configure your local AI models, Whisper speech-to-text resolution, and candidate profile preferences.
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

      <form onSubmit={handleSave} className="space-y-8">
        {/* AI Provider & Models */}
        <div className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
              <Cpu className="w-4 h-4 text-indigo-400" />
              <span>AI Model Abstraction Layer</span>
            </h2>
            <div className="flex items-center gap-2">
              <span
                className={`w-2.5 h-2.5 rounded-full ${
                  ollamaAvailable ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'
                }`}
              />
              <span className="text-xs text-slate-400">
                {ollamaAvailable ? 'Ollama Online' : 'Ollama Unreachable'}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-2">
              <label className="text-xs font-semibold text-slate-300">Model Provider</label>
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white focus:outline-none focus:border-indigo-500 transition-colors"
              >
                <option value="ollama">Ollama (Local / Free / Private)</option>
                <option value="openai_compatible">OpenAI-Compatible / LMStudio / OpenRouter</option>
              </select>
            </div>

            <div className="space-y-2">
              <label className="text-xs font-semibold text-slate-300">Inference LLM Model</label>
              {availableModels.length > 0 ? (
                <select
                  value={modelName}
                  onChange={(e) => setModelName(e.target.value)}
                  className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white focus:outline-none focus:border-indigo-500 transition-colors"
                >
                  {availableModels.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={modelName}
                  onChange={(e) => setModelName(e.target.value)}
                  placeholder="e.g. llama3.2:3b, mistral, qwen2.5-coder"
                  className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
                />
              )}
            </div>

            <div className="space-y-2">
              <label className="text-xs font-semibold text-slate-300">Embedding Model (for RAG)</label>
              <input
                type="text"
                value={embeddingModel}
                onChange={(e) => setEmbeddingModel(e.target.value)}
                placeholder="e.g. nomic-embed-text, all-MiniLM-L6-v2"
                className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>

            <div className="space-y-2">
              <label className="text-xs font-semibold text-slate-300">Whisper Speech Model Size</label>
              <select
                value={transcriptionModel}
                onChange={(e) => setTranscriptionModel(e.target.value)}
                className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white focus:outline-none focus:border-indigo-500 transition-colors"
              >
                <option value="tiny">Tiny (~75MB, Ultra Fast / Low RAM)</option>
                <option value="base">Base (~145MB, Balanced Default)</option>
                <option value="small">Small (~460MB, Higher Accuracy)</option>
                <option value="medium">Medium (~1.5GB, High Fidelity)</option>
              </select>
            </div>
          </div>
        </div>

        {/* Candidate Profile Details */}
        <div className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-6">
          <h2 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
            <Sliders className="w-4 h-4 text-indigo-400" />
            <span>Candidate Profile Defaults</span>
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-2">
              <label className="text-xs font-semibold text-slate-300">Your Full Name</label>
              <input
                type="text"
                value={candidateName}
                onChange={(e) => setCandidateName(e.target.value)}
                placeholder="e.g. Alex Morgan"
                className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>

            <div className="space-y-2">
              <label className="text-xs font-semibold text-slate-300">Target Role Title</label>
              <input
                type="text"
                value={targetRole}
                onChange={(e) => setTargetRole(e.target.value)}
                placeholder="e.g. Senior Full-Stack Engineer"
                className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
          </div>
        </div>

        {/* Privacy Model Notice */}
        <div className="glass-panel p-6 rounded-2xl border border-emerald-500/30 bg-emerald-950/10 space-y-2 text-xs">
          <div className="flex items-center gap-2 font-bold text-emerald-400">
            <ShieldCheck className="w-4 h-4" />
            <span>Local-First & Data Retention Guarantee</span>
          </div>
          <p className="text-slate-300 leading-relaxed">
            All resume data, transcripts, embeddings, and mock evaluations reside exclusively in your local SQLite and ChromaDB files. No external telemetry or cloud uploads occur without explicit remote provider configuration.
          </p>
        </div>

        <div className="flex justify-end pt-4 border-t border-slate-800">
          <button
            type="submit"
            disabled={saving}
            className="flex items-center gap-2 px-6 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition-all shadow-lg shadow-indigo-600/30 disabled:opacity-50"
          >
            {saving ? (
              <span className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              <CheckCircle2 className="w-4 h-4" />
            )}
            <span>Save Configuration</span>
          </button>
        </div>
      </form>
    </div>
  );
}
