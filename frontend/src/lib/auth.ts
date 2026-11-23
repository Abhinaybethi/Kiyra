/**
 * Strict email-OTP auth for the web client.
 *
 * The token is opaque, comes only from POST /api/auth/verify, and is kept in
 * localStorage for this browser session. There is no stored-session
 * auto-login on the desktop overlay; the web client keeps the token so page
 * navigation does not force a new email, but a 401 from any endpoint clears it
 * and sends the user back to /login.
 */
const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export const AUTH_TOKEN_KEY = 'kiyra.auth.token';
export const AUTH_EVENT = 'kiyra:auth';

export interface AuthAccount {
  id: number;
  full_name: string;
  phone: string;
  email: string;
  is_verified: boolean;
  created_at: string;
  last_login_at?: string | null;
}

export interface AuthSession {
  token: string;
  token_type: string;
  expires_at: string;
  account: AuthAccount;
}

export interface AuthChallenge {
  ok: boolean;
  purpose: 'register' | 'login';
  email: string;
  delivery: 'email';
  otp_length: number;
  expires_in: number;
  resend_in: number;
  message: string;
}

export interface AuthStatus {
  enabled: boolean;
  /** Local-only opt-in: the backend accepts requests without a token. */
  dev_bypass: boolean;
  email_delivery_required: boolean;
  smtp_configured: boolean;
  otp_length: number;
  otp_ttl_seconds: number;
  resend_cooldown_seconds: number;
  session_days: number;
}

export function getAuthToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

function emitAuth(reason: 'signed-in' | 'signed-out' | 'unauthorized'): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(AUTH_EVENT, { detail: { reason } }));
}

export function setAuthToken(token: string): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(AUTH_TOKEN_KEY, token);
  emitAuth('signed-in');
}

export function clearAuthToken(reason: 'signed-out' | 'unauthorized' = 'signed-out'): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(AUTH_TOKEN_KEY);
  emitAuth(reason);
}

export function authHeaders(extra: HeadersInit | undefined = undefined): Record<string, string> {
  const headers: Record<string, string> = {};
  if (extra) {
    new Headers(extra).forEach((value, key) => { headers[key] = value; });
  }
  const token = getAuthToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

/** Browser WebSocket API cannot set headers; the same bearer token goes in the query string. */
export function websocketTokenParam(): string {
  const token = getAuthToken();
  return token ? `?token=${encodeURIComponent(token)}` : '';
}

async function publicRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });

  if (!res.ok) {
    if (res.status === 401) clearAuthToken('unauthorized');
    let detail = `Request failed: ${res.statusText}`;
    try {
      const err = await res.json();
      detail = err.detail || detail;
    } catch {
      // fallback
    }
    throw new Error(detail);
  }
  return res.json();
}

export const authApi = {
  status: () => publicRequest<AuthStatus>('/api/auth/status'),
  register: (data: { full_name: string; phone: string; email: string }) =>
    publicRequest<AuthChallenge>('/api/auth/register', {
      method: 'POST', body: JSON.stringify(data),
    }),
  login: (email: string) =>
    publicRequest<AuthChallenge>('/api/auth/login', {
      method: 'POST', body: JSON.stringify({ email }),
    }),
  resend: (email: string, purpose: 'register' | 'login') =>
    publicRequest<AuthChallenge>('/api/auth/resend', {
      method: 'POST', body: JSON.stringify({ email, purpose }),
    }),
  verify: (email: string, code: string, purpose: 'register' | 'login') =>
    publicRequest<AuthSession>('/api/auth/verify', {
      method: 'POST', body: JSON.stringify({ email, code, purpose }),
    }),
  me: () => publicRequest<AuthAccount>('/api/auth/me', {
    headers: authHeaders(),
  }),
  logout: async () => {
    const res = await fetch(`${API_BASE}/api/auth/logout`, {
      method: 'POST', headers: authHeaders(),
    });
    if (!res.ok) {
      if (res.status === 401) clearAuthToken('unauthorized');
      throw new Error('Sign-out failed');
    }
    return res.json() as Promise<{ ok: boolean }>;
  },
};
