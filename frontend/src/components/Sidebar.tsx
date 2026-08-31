'use client';

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutDashboard,
  Bot,
  Radio,
  FileText,
  Briefcase,
  Database,
  History,
  BarChart3,
  Settings,
  Sparkles,
  Zap,
} from 'lucide-react';

const navigation = [
  { name: 'Dashboard', href: '/', icon: LayoutDashboard },
  { name: 'Practice Mode', href: '/practice', icon: Bot, badge: 'AI Mock' },
  { name: 'Live Assistant', href: '/live', icon: Radio, badge: 'Realtime' },
  { name: 'Resume & Context', href: '/resume', icon: FileText },
  { name: 'Job Descriptions', href: '/jobs', icon: Briefcase },
  { name: 'Knowledge Base', href: '/knowledge', icon: Database, badge: 'RAG' },
  { name: 'Interview History', href: '/history', icon: History },
  { name: 'Analytics', href: '/analytics', icon: BarChart3 },
  { name: 'Settings & Models', href: '/settings', icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-64 flex-shrink-0 border-r border-slate-800/80 bg-slate-950/60 backdrop-blur-xl flex flex-col justify-between p-4 min-h-screen sticky top-0 z-30">
      <div>
        {/* Brand Logo */}
        <Link href="/" className="flex items-center gap-3 px-3 py-4 mb-6 group">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-indigo-500/20 group-hover:scale-105 transition-transform duration-200">
            <Sparkles className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="font-bold text-base tracking-tight text-white flex items-center gap-1.5">
              Interview<span className="text-indigo-400">AI</span>
            </h1>
            <p className="text-[11px] text-slate-400 font-medium">Local-First Intelligence</p>
          </div>
        </Link>

        {/* Navigation List */}
        <nav className="space-y-1">
          {navigation.map((item) => {
            const isActive = pathname === item.href || (item.href !== '/' && pathname.startsWith(item.href));
            const Icon = item.icon;

            return (
              <Link
                key={item.name}
                href={item.href}
                className={`flex items-center justify-between px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all duration-150 ${
                  isActive
                    ? 'bg-indigo-600/15 text-indigo-400 border border-indigo-500/30 shadow-sm'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
                }`}
              >
                <div className="flex items-center gap-3">
                  <Icon className={`w-4 h-4 ${isActive ? 'text-indigo-400' : 'text-slate-400'}`} />
                  <span>{item.name}</span>
                </div>
                {item.badge && (
                  <span
                    className={`text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full ${
                      isActive
                        ? 'bg-indigo-500/20 text-indigo-300'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    {item.badge}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
      </div>

      {/* Local Mode Badge */}
      <div className="mt-auto pt-4 border-t border-slate-800/60">
        <div className="p-3 rounded-xl bg-slate-900/80 border border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse" />
            <div>
              <p className="text-xs font-semibold text-slate-200">Local Engine</p>
              <p className="text-[11px] text-slate-500">100% Free & Private</p>
            </div>
          </div>
          <Zap className="w-4 h-4 text-amber-400" />
        </div>
      </div>
    </aside>
  );
}
