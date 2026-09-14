import type {
  AdminAccount,
  AdminAccountsResponse,
  AdminExportConversationListResponse,
  AdminUsageSessionDetail,
  AuthMeResponse,
  AuthResponse,
} from '../types/auth';
import { getApiBase } from '../utils/format';
import { userFacingErrorMessage } from '../utils/displayText';
import { getCurrentLanguage } from '../i18n/LanguageContext';
import { ApiError } from './errors';

function localizedJsonHeaders(init?: HeadersInit): Headers {
  const headers = new Headers(init);
  headers.set('Accept-Language', getCurrentLanguage());
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return headers;
}

async function parseResponse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

async function authRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${getApiBase()}${path}`, {
    ...options,
    credentials: 'include',
    headers: localizedJsonHeaders(options.headers),
  });
  const payload = await parseResponse(response);
  if (!response.ok) {
    throw new ApiError(
      userFacingErrorMessage(payload, response.status),
      response.status,
      payload,
      path,
    );
  }
  return payload as T;
}

export interface UserContentDownload {
  blob: Blob;
  filename: string;
}

export interface UserContentExportFilter {
  user_emails: string[];
  session_ids?: string[];
  start_date?: string | null;
  end_date?: string | null;
}

function downloadFilename(response: Response): string {
  const disposition = response.headers.get('content-disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      return encoded;
    }
  }
  return (
    disposition.match(/filename="?([^";]+)"?/i)?.[1]
    || 'hr_agent_user_content.zip'
  );
}

async function downloadUserContent(
  filters: UserContentExportFilter = { user_emails: [] },
): Promise<UserContentDownload> {
  const response = await fetch(`${getApiBase()}/admin/exports/user-content`, {
    method: 'POST',
    credentials: 'include',
    headers: localizedJsonHeaders(),
    body: JSON.stringify(filters),
  });
  if (!response.ok) {
    const payload = await parseResponse(response);
    throw new ApiError(
      userFacingErrorMessage(payload, response.status),
      response.status,
      payload,
      '/admin/exports/user-content',
    );
  }
  return {
    blob: await response.blob(),
    filename: downloadFilename(response),
  };
}

export const authApi = {
  login: (email: string, password: string) => authRequest<AuthResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  }),
  register: (email: string, password: string, displayName?: string) => authRequest<AuthResponse>('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password, display_name: displayName || null }),
  }),
  getMe: () => authRequest<AuthMeResponse>('/auth/me'),
  logout: () => authRequest<AuthResponse>('/auth/logout', { method: 'POST', body: '{}' }),
  listAccounts: () => authRequest<AdminAccountsResponse>('/auth/admin/accounts'),
  listExportConversations: () => authRequest<AdminExportConversationListResponse>(
    '/admin/exports/user-content/sessions',
  ),
  getUsageSessionDetail: (sessionId: string) => authRequest<AdminUsageSessionDetail>(
    `/admin/exports/user-content/sessions/${encodeURIComponent(sessionId)}`,
  ),
  createAccount: (email: string, password: string, displayName?: string) => authRequest<AdminAccount>('/auth/admin/accounts', {
    method: 'POST',
    body: JSON.stringify({ email, password, display_name: displayName || null }),
  }),
  deleteAccount: (email: string) => authRequest<AuthResponse>(`/auth/admin/accounts/${encodeURIComponent(email)}`, {
    method: 'DELETE',
  }),
  resetPassword: (email: string, password: string) => authRequest<AdminAccount>(
    `/auth/admin/accounts/${encodeURIComponent(email)}/password`,
    { method: 'PATCH', body: JSON.stringify({ password }) },
  ),
  downloadUserContent,
  updateWhitelist: (email: string, enabled: boolean) => authRequest<AdminAccount>('/auth/admin/whitelist', {
    method: 'PUT',
    body: JSON.stringify({ email, enabled }),
  }),
};
