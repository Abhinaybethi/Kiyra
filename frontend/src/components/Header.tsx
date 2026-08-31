'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { Bot, Cpu, Radio, ShieldCheck } from 'lucide-react';
import { api, ModelConfig, Profile } from '@/lib/api';

export function Header() {
  const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [isHealthy, setIsHealthy] = useState<boolean>(true);

  useEffect(() => {
    api.getModelConfig().then(setModelConfig).catch(() => {});
    api.getProfile().then(setProfile).catch(() => {});
    api.getModelHealth().then((h) => setIsHealthy(h.healthy)).catch(() => setIsHealthy(false));
  }, []);

  return (
    <header className="h-16 border-b border-slate-800/80 bg-slate-950/40 backdrop-blur-md px-6 flex items-center justify-between sticky top-0 z-20">
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2 text-xs text-slate-400 bg-slate-900/90 border border-slate-800 px-3 py-1.5 rounded-lg">
          <Cpu className="w-3.5 h-3.5 text-indigo-400" />
          <span>Active LLM:</span>
          <span className="font-mono text-slate-200 font-semibold">
            {modelConfig?.model_name || 'llama3.2:3b'}
          </span>
          <span className={`w-2 h-2 rounded-full ${isHealthy ? 'bg-emerald-400' : 'bg-amber-400'}`} />
        </div>

        <div className="hidden sm:flex items-center gap-1.5 text-xs text-emerald-400/90 bg-emerald-500/10 border border-emerald-500/20 px-2.5 py-1 rounded-md">
          <ShieldCheck className="w-3.5 h-3.5" />
          <span>Offline / Local Storage</span>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Link
          href="/practice"
          className="flex items-center gap-2 text-xs font-semibold px-3.5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white transition-colors shadow-sm shadow-indigo-600/30"
        >
          <Bot className="w-3.5 h-3.5" />
          <span>New Practice</span>
        </Link>
        <Link
          href="/live"
          className="flex items-center gap-2 text-xs font-semibold px-3.5 py-2 rounded-lg bg-rose-600/20 hover:bg-rose-600/30 text-rose-300 border border-rose-500/30 transition-colors"
        >
          <Radio className="w-3.5 h-3.5 animate-pulse" />
          <span>Live Assistant</span>
        </Link>
        {profile?.name && (
          <div className="hidden md:flex items-center gap-2 pl-3 border-l border-slate-800">
            <div className="w-7 h-7 rounded-full bg-gradient-to-r from-purple-500 to-indigo-500 flex items-center justify-center text-[11px] font-bold text-white uppercase">
              {profile.name[0]}
            </div>
            <span className="text-xs text-slate-300 font-medium">{profile.name}</span>
          </div>
        )}
      </div>
    </header>
  );
}
