import type {
  AdminTestWorkflowCreatePayload,
  AsrReadinessResponse,
  CoachReport,
  DocumentRecord,
  EmployeeLatestSetupSettingsResponse,
  EmployeeSearchResponse,
  EmployeeProfile,
  BigFivePersonality,
  GuidanceReport,
  IntentGoalPerformanceItem,
  IntentPerformanceDraftResponse,
  RehearsalContextUpdatePayload,
  RehearsalSpeechRequest,
  SessionLocale,
  SessionState,
  SetupOptions,
} from '../types/domain';
import { getApiBase } from '../utils/format';
import { userFacingErrorMessage } from '../utils/displayText';
import { getCurrentLanguage } from '../i18n/LanguageContext';
import { ApiError } from './errors';

export { ApiError } from './errors';

function reportUnauthorized(response: Response) {
  if (response.status === 401) {
    window.dispatchEvent(new Event('hr-auth-expired'));
  }
}
const REQUEST_TIMEOUT_MS = 180000;

function localizedHeaders(init?: HeadersInit, json = false): Headers {
  const headers = new Headers(init);
  headers.set('Accept-Language', getCurrentLanguage());
  if (json && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  return headers;
}

export interface FileDownload {
  blob: Blob;
  filename: string;
}

export interface ResourceChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

export interface ResourceChatSource {
  id: string;
  title: string;
}

export interface ResourceChatRequest {
  message: string;
  history: ResourceChatTurn[];
}

export interface ResourceChatResponse {
  answer: string;
  sources: ResourceChatSource[];
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

function detailFromPayload(payload: unknown): string {
  return userFacingErrorMessage(payload);
}

function errorFromResponse(response: Response, payload: unknown, path: string): ApiError {
  return new ApiError(
    userFacingErrorMessage(payload, response.status),
    response.status,
    payload,
    path,
  );
}

async function requestJson<T>(path: string, options: RequestInit & { timeoutMs?: number | null } = {}): Promise<T> {
  const controller = new AbortController();
  const { timeoutMs = REQUEST_TIMEOUT_MS, signal: externalSignal, ...fetchOptions } = options;
  const forwardAbort = () => controller.abort();
  if (externalSignal?.aborted) {
    forwardAbort();
  } else {
    externalSignal?.addEventListener('abort', forwardAbort, { once: true });
  }
  const timeout = typeof timeoutMs === 'number' && timeoutMs > 0
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;
  try {
    const response = await fetch(getApiBase() + path, {
      ...fetchOptions,
      credentials: 'include',
      signal: controller.signal,
      headers: localizedHeaders(fetchOptions.headers, true),
    });
    reportUnauthorized(response);
    const payload = await parseResponse(response);
    if (!response.ok) throw errorFromResponse(response, payload, path);
    return payload as T;
  } finally {
    if (timeout !== null) window.clearTimeout(timeout);
    externalSignal?.removeEventListener('abort', forwardAbort);
  }
}

async function requestForm<T>(path: string, body: FormData): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(getApiBase() + path, {
      method: 'POST',
      credentials: 'include',
      body,
      signal: controller.signal,
      headers: localizedHeaders(),
    });
    reportUnauthorized(response);
    const payload = await parseResponse(response);
    if (!response.ok) throw errorFromResponse(response, payload, path);
    return payload as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

function downloadFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get('content-disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      return encoded;
    }
  }
  return disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
}

async function requestDownload(path: string, fallback: string): Promise<FileDownload> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(getApiBase() + path, {
      credentials: 'include',
      signal: controller.signal,
      headers: localizedHeaders(),
    });
    reportUnauthorized(response);
    if (!response.ok) {
      const payload = await parseResponse(response);
      throw errorFromResponse(response, payload, path);
    }
    return {
      blob: await response.blob(),
      filename: downloadFilename(response, fallback),
    };
  } finally {
    window.clearTimeout(timeout);
  }
}

async function streamSse<T = Record<string, unknown>>(
  path: string,
  options: RequestInit,
  onEvent: (event: string, data: T) => void,
): Promise<void> {
  const response = await fetch(getApiBase() + path, {
    ...options,
    credentials: 'include',
    headers: localizedHeaders(options.headers, true),
  });
  reportUnauthorized(response);
  if (!response.ok) {
    const payload = await parseResponse(response);
    throw errorFromResponse(response, payload, path);
  }
  if (!response.body) throw new Error('当前浏览器不支持流式响应。');

  const dispatchChunk = (chunk: string) => {
    let eventName = 'message';
    const dataLines: string[] = [];
    for (const line of chunk.split(/\r\n|\r|\n/)) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    const dataText = dataLines.join('\n') || '{}';
    const data = JSON.parse(dataText) as T;
    if (eventName === 'error') throw new Error(detailFromPayload(data));
    onEvent(eventName, data);
  };

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const eventBoundary = /(?:\r\n|\r|\n)(?:\r\n|\r|\n)/;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let boundary = eventBoundary.exec(buffer);
    while (boundary?.index !== undefined) {
      const chunk = buffer.slice(0, boundary.index);
      if (chunk.trim()) dispatchChunk(chunk);
      buffer = buffer.slice(boundary.index + boundary[0].length);
      boundary = eventBoundary.exec(buffer);
    }
    if (done) {
      if (buffer.trim()) dispatchChunk(buffer);
      break;
    }
  }
}

export const api = {
  sendResourceChatMessage: (payload: ResourceChatRequest, signal?: AbortSignal) => requestJson<ResourceChatResponse>(
    '/resource-chat/message',
    { method: 'POST', body: JSON.stringify(payload), signal },
  ),
  asrReadiness: () => requestJson<AsrReadinessResponse>('/asr/readiness', { timeoutMs: 3000 }),
  createSession: (locale: SessionLocale = getCurrentLanguage()) => requestJson<SessionState>(
    '/sessions',
    { method: 'POST', body: JSON.stringify({ locale }), timeoutMs: 15000 },
  ),
  getSession: (sessionId: string) => requestJson<SessionState>(`/sessions/${sessionId}`, { timeoutMs: 30000 }),
  updateSessionLocale: (sessionId: string, locale: SessionLocale) => requestJson<SessionState>(
    `/sessions/${encodeURIComponent(sessionId)}/locale`,
    { method: 'PATCH', body: JSON.stringify({ locale }), timeoutMs: 15000 },
  ),
  setupOptions: () => requestJson<SetupOptions>('/setup/options', { timeoutMs: 30000 }),
  createAdminTestWorkflow: (payload: AdminTestWorkflowCreatePayload) => requestJson<SessionState>(
    '/admin/test-workflows',
    {
      method: 'POST',
      body: JSON.stringify({ ...payload, locale: payload.locale || getCurrentLanguage() }),
      timeoutMs: 30000,
    },
  ),
  searchEmployees: (params: URLSearchParams, signal?: AbortSignal) => {
    const query = params.toString();
    return requestJson<EmployeeSearchResponse>(`/employees${query ? `?${query}` : ''}`, {
      timeoutMs: 30000,
      signal,
    });
  },
  getLatestEmployeeSetupSettings: (employeeId: string) =>
    requestJson<EmployeeLatestSetupSettingsResponse>(
      `/setup/employees/${encodeURIComponent(employeeId)}/latest-settings`,
      { timeoutMs: 15000 },
    ),
  uploadDocument: (formData: FormData) => requestForm<DocumentRecord>('/documents/upload', formData),
  uploadText: (sessionId: string | null, text: string) => requestJson<DocumentRecord>('/documents/text', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, text, filename: 'session_supplemental_info.txt' }),
  }),
  confirmProfile: (sessionId: string, profile: EmployeeProfile) => requestJson<SessionState>(`/setup/${sessionId}/profile`, {
    method: 'PATCH',
    body: JSON.stringify({ profile }),
  }),
  generateIntentPerformanceDraft: (sessionId: string, intentId: string) => requestJson<IntentPerformanceDraftResponse>(
    `/setup/${sessionId}/intent-performance-draft`,
    {
      method: 'POST',
      body: JSON.stringify({ intent_id: intentId }),
    },
  ),
  streamIntentPerformanceDraft: (
    sessionId: string,
    intentId: string,
    onEvent: (event: string, data: Record<string, unknown>) => void,
    signal?: AbortSignal,
  ) => streamSse(
    `/setup/${sessionId}/intent-performance-draft/stream`,
    {
      method: 'POST',
      body: JSON.stringify({ intent_id: intentId }),
      signal,
    },
    onEvent,
  ),
  confirmIntent: (
    sessionId: string,
    intentId: string,
    performanceItems: IntentGoalPerformanceItem[],
  ) => requestJson<SessionState>(`/setup/${sessionId}/intent`, {
    method: 'PATCH',
    body: JSON.stringify({
      intent_id: intentId,
      performance_items: performanceItems,
    }),
  }),
  confirmSimulation: (sessionId: string, personality: BigFivePersonality, primaryMotiveId: string, secondaryMotiveIds: string[]) => requestJson<SessionState>(`/setup/${sessionId}/simulation`, {
    method: 'PATCH',
    body: JSON.stringify({
      personality,
      primary_motive_id: primaryMotiveId,
      secondary_motive_ids: secondaryMotiveIds,
      run_mode: 'guidance_then_rehearsal',
    }),
  }),
  completeSetup: (sessionId: string) => requestJson<SessionState>(`/setup/${sessionId}/complete`, { method: 'POST', body: '{}' }),
  getGuidance: (sessionId: string) => requestJson<GuidanceReport>(`/guidance/${sessionId}`),
  generateGuidance: (sessionId: string) => requestJson<GuidanceReport>(`/guidance/${sessionId}`, { method: 'POST', body: '{}' }),
  streamGuidance: (sessionId: string, onEvent: (event: string, data: Record<string, unknown>) => void) =>
    streamSse(`/guidance/${sessionId}/stream`, { method: 'POST', body: '{}' }, onEvent),
  downloadGuidanceWord: (sessionId: string) => requestDownload(
    `/guidance/${encodeURIComponent(sessionId)}/export.docx`,
    'guidance.docx',
  ),
  streamRehearsalMessage: (
    sessionId: string,
    message: string,
    onEvent: (event: string, data: Record<string, unknown>) => void,
    speech?: RehearsalSpeechRequest,
    requestId?: string,
  ) => streamSse(
    `/rehearsal/${sessionId}/message/stream`,
    { method: 'POST', body: JSON.stringify({ message, speech, request_id: requestId }) },
    onEvent,
  ),
  sendRehearsalMessage: (sessionId: string, message: string, requestId?: string) => requestJson<SessionState>(`/rehearsal/${sessionId}/message`, {
    method: 'POST',
    body: JSON.stringify({ message, request_id: requestId }),
    timeoutMs: null,
  }),
  updateRehearsalContext: (sessionId: string, payload: RehearsalContextUpdatePayload) => requestJson<SessionState>(`/rehearsal/${sessionId}/context`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  }),
  endRehearsal: (sessionId: string) => requestJson<SessionState>(`/rehearsal/${sessionId}/end`, { method: 'POST', body: '{}' }),
  getCoachReport: (sessionId: string) => requestJson<CoachReport>(`/reports/${sessionId}/coach`),
  streamCoachReport: (sessionId: string, onEvent: (event: string, data: Record<string, unknown>) => void) =>
    streamSse(`/reports/${sessionId}/coach/stream`, { method: 'POST', body: '{}' }, onEvent),
  generateCoachReport: (sessionId: string) => requestJson<CoachReport>(`/reports/${sessionId}/coach`, { method: 'POST', body: '{}', timeoutMs: null }),
};
