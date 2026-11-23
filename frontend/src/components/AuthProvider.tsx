'use client';

import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
  AUTH_EVENT,
  authApi,
  clearAuthToken,
  getAuthToken,
  type AuthAccount,
} from '@/lib/auth';

interface AuthContextValue {
  account: AuthAccount | null;
  ready: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  account: null,
  ready: false,
  signOut: async () => {},
});

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [account, setAccount] = useState<AuthAccount | null>(null);
  const [ready, setReady] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const onLoginPage = pathname === '/login';

  useEffect(() => {
    if (onLoginPage) {
      setReady(true);
      return;
    }

    const token = getAuthToken();
    if (!token) {
      setAccount(null);
      setReady(true);
      router.replace('/login');
      return;
    }

    let cancelled = false;
    setReady(false);
    authApi.me()
      .then((value) => { if (!cancelled) setAccount(value); })
      .catch(() => {
        if (cancelled) return;
        clearAuthToken('unauthorized');
        setAccount(null);
        router.replace('/login');
      })
      .finally(() => { if (!cancelled) setReady(true); });

    return () => { cancelled = true; };
  }, [onLoginPage, router]);

  useEffect(() => {
    const handler = (event: Event) => {
      const reason = (event as CustomEvent<{ reason?: string }>).detail?.reason;
      if (reason === 'unauthorized' || reason === 'signed-out') {
        setAccount(null);
        if (window.location.pathname !== '/login') router.replace('/login');
      }
    };
    window.addEventListener(AUTH_EVENT, handler);
    return () => window.removeEventListener(AUTH_EVENT, handler);
  }, [router]);

  const signOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Even if the backend already revoked the session, drop the local token.
    }
    clearAuthToken('signed-out');
    setAccount(null);
    router.replace('/login');
  }, [router]);

  return (
    <AuthContext.Provider value={{ account, ready, signOut }}>
      {ready || onLoginPage ? children : (
        <div className="min-h-screen flex items-center justify-center">
          <div className="flex flex-col items-center gap-3">
            <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
            <p className="text-sm text-slate-400">Verifying your session…</p>
          </div>
        </div>
      )}
    </AuthContext.Provider>
  );
}
