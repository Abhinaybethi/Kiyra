'use client';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowLeft, ArrowRight, Mail, LockKeyhole, ShieldCheck } from 'lucide-react';
import {
  authApi,
  clearAuthToken,
  getAuthToken,
  setAuthToken,
  type AuthChallenge,
  type AuthStatus,
} from '@/lib/auth';

type Mode = 'login' | 'register';
type Purpose = 'login' | 'register';

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Try again.';
}

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>('login');
  const [fullName, setFullName] = useState('');
  const [phone, setPhone] = useState('');
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState<AuthChallenge | null>(null);
  const [resendIn, setResendIn] = useState(0);
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    authApi.status().then(setStatus).catch((e) => setError(messageOf(e)));
    if (getAuthToken()) {
      authApi.me().then(() => router.replace('/')).catch(() => clearAuthToken('unauthorized'));
    }
  }, [router]);

  useEffect(() => {
    if (resendIn <= 0) return;
    const timer = window.setInterval(() => setResendIn((n) => Math.max(0, n - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [resendIn]);

  const requestCode = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = mode === 'register'
        ? await authApi.register({ full_name: fullName.trim(), phone: phone.trim(), email: email.trim() })
        : await authApi.login(email.trim());
      setChallenge(result);
      setCode('');
      setResendIn(result.resend_in || 0);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };

  const verifyCode = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!challenge) return;
    const digits = code.trim();
    if (!/^\d+$/.test(digits) || digits.length !== challenge.otp_length) {
      setError(`Enter the ${challenge.otp_length}-digit code from your email.`);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const session = await authApi.verify(
        challenge.email, digits, challenge.purpose as Purpose);
      setAuthToken(session.token);
      router.replace('/');
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    if (!challenge || resendIn > 0) return;
    setError(null);
    setBusy(true);
    try {
      const result = await authApi.resend(challenge.email, challenge.purpose as Purpose);
      setChallenge(result);
      setCode('');
      setResendIn(result.resend_in || 0);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };

  const switchMode = (next: Mode) => {
    setMode(next);
    setChallenge(null);
    setCode('');
    setResendIn(0);
    setError(null);
  };

  const otpLength = challenge?.otp_length || status?.otp_length || 6;

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-slate-900/70 p-8 shadow-2xl shadow-black/40">
        <div className="flex items-center gap-2 text-emerald-400 text-xs font-semibold uppercase tracking-widest">
          <ShieldCheck className="h-4 w-4" />
          Live verification
        </div>
        <h1 className="mt-3 text-2xl font-bold text-white">
          {challenge ? 'Check your email' : mode === 'login' ? 'Welcome back' : 'Create your account'}
        </h1>
        <p className="mt-2 text-sm text-slate-400">
          {challenge
            ? challenge.message
            : 'Every sign-in requires a fresh one-time code sent to your inbox.'}
        </p>

        {status && !status.smtp_configured && (
          <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
            Email delivery is not configured on this backend. Set SMTP_HOST,
            SMTP_USERNAME, SMTP_PASSWORD and SMTP_FROM in <code>backend/.env</code>,
            then restart the server.
          </div>
        )}

        {error && (
          <div className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
            {error}
          </div>
        )}

        {!challenge ? (
          <form onSubmit={requestCode} className="mt-6 space-y-4">
            {mode === 'register' && (
              <>
                <Field label="Full name" value={fullName} onChange={setFullName} placeholder="Ada Lovelace" />
                <Field label="Phone" value={phone} onChange={setPhone} placeholder="+91 98765 43210" />
              </>
            )}
            <Field label="Email" type="email" value={email} onChange={setEmail} placeholder="you@example.com" />
            <button
              type="submit"
              disabled={busy}
              className="w-full flex items-center justify-center gap-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-4 py-3 text-sm font-semibold text-white transition-colors"
            >
              <Mail className="h-4 w-4" />
              {busy ? 'Sending code…' : 'Send verification code'}
              {!busy && <ArrowRight className="h-4 w-4" />}
            </button>
            <p className="text-center text-xs text-slate-400">
              {mode === 'login' ? 'New here? ' : 'Already have an account? '}
              <button
                type="button"
                onClick={() => switchMode(mode === 'login' ? 'register' : 'login')}
                className="font-semibold text-indigo-400 hover:underline"
              >
                {mode === 'login' ? 'Create an account' : 'Sign in'}
              </button>
            </p>
          </form>
        ) : (
          <form onSubmit={verifyCode} className="mt-6 space-y-4">
            <div>
              <label htmlFor="otp" className="block text-xs font-medium text-slate-300 mb-2">
                {otpLength}-digit code
              </label>
              <div className="flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-950/60 px-3">
                <LockKeyhole className="h-4 w-4 text-slate-500" />
                <input
                  id="otp"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={otpLength}
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, otpLength))}
                  placeholder={'0'.repeat(otpLength)}
                  className="w-full bg-transparent py-3 font-mono text-lg tracking-[0.3em] text-white outline-none placeholder:text-slate-700"
                />
              </div>
            </div>
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-xl bg-emerald-500 hover:bg-emerald-400 disabled:opacity-50 px-4 py-3 text-sm font-semibold text-slate-950 transition-colors"
            >
              {busy ? 'Verifying…' : 'Verify and continue'}
            </button>
            <div className="flex items-center justify-between text-xs">
              <button
                type="button"
                onClick={() => { setChallenge(null); setCode(''); setError(null); }}
                className="flex items-center gap-1 text-slate-400 hover:text-slate-200"
              >
                <ArrowLeft className="h-3.5 w-3.5" /> Use another email
              </button>
              <button
                type="button"
                onClick={resend}
                disabled={resendIn > 0 || busy}
                className="font-semibold text-indigo-400 hover:underline disabled:text-slate-600 disabled:no-underline"
              >
                {resendIn > 0 ? `Resend in ${resendIn}s` : 'Resend code'}
              </button>
            </div>
            <p className="text-center text-[11px] text-slate-500">
              The code was sent only to {challenge.email}. Check spam if it does not arrive.
            </p>
          </form>
        )}
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-300 mb-2">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-xl border border-slate-700 bg-slate-950/60 px-3 py-3 text-sm text-white outline-none focus:border-indigo-500 placeholder:text-slate-600"
      />
    </div>
  );
}
