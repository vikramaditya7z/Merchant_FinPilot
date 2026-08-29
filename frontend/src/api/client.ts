import {
  AuditTrailResponse,
  HealthResponse,
  ProcessIncidentResponse,
  ScenarioMetadata,
  StageProgressEvent,
} from './types';

// In local development, connect directly to Python WSGI server at port 8000 via CORS
const API_BASE =
  typeof window !== 'undefined' &&
  (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
    ? 'http://127.0.0.1:8000/api/v1'
    : '/api/v1';

async function parseResponse<T = any>(res: Response, fallbackMessage: string): Promise<T> {
  const text = await res.text();
  let data: any = null;
  if (text && text.trim()) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text };
    }
  }

  if (!res.ok) {
    const errMsg =
      (data && (data.error || data.detail || data.message)) ||
      `${fallbackMessage} (HTTP ${res.status})`;
    throw new Error(errMsg);
  }

  if (data === null) {
    throw new Error(`Empty response received from server (HTTP ${res.status})`);
  }

  return data as T;
}

export class FinPilotApiClient {
  async getHealth(): Promise<HealthResponse> {
    const res = await fetch(`${API_BASE}/health`);
    return parseResponse<HealthResponse>(res, 'Health check failed');
  }

  async listScenarios(): Promise<ScenarioMetadata[]> {
    const res = await fetch(`${API_BASE}/scenarios`);
    const data = await parseResponse<{ scenarios: ScenarioMetadata[] }>(
      res,
      'Failed to fetch scenarios'
    );
    return data.scenarios || [];
  }

  async processIncident(params: {
    merchant_id: string;
    scenario_id?: string;
    incident_id?: string;
    context_notes?: string;
  }): Promise<ProcessIncidentResponse> {
    const res = await fetch(`${API_BASE}/incidents/process`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(params),
    });

    return parseResponse<ProcessIncidentResponse>(res, 'Process incident failed');
  }

  async processIncidentStream(
    params: {
      merchant_id: string;
      scenario_id?: string;
      incident_id?: string;
      context_notes?: string;
    },
    onEvent: (event: StageProgressEvent) => void,
    signal?: AbortSignal
  ): Promise<ProcessIncidentResponse> {
    const res = await fetch(`${API_BASE}/incidents/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(params),
      signal,
    });

    if (!res.ok) {
      return parseResponse<ProcessIncidentResponse>(res, 'Streaming process incident failed');
    }

    if (!res.body) {
      throw new Error('Response stream body is unavailable');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let finalPayload: ProcessIncidentResponse | null = null;
    let streamError: string | null = null;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.startsWith('data:')) {
            const jsonStr = trimmed.slice(5).trim();
            if (jsonStr) {
              try {
                const event: StageProgressEvent = JSON.parse(jsonStr);
                onEvent(event);
                if (event.stage === 'pipeline') {
                  if (event.payload) {
                    finalPayload = event.payload;
                  }
                  if (event.status === 'failed' && event.details) {
                    streamError = event.details;
                  }
                }
              } catch (e) {
                console.warn('Failed to parse SSE JSON event:', jsonStr, e);
              }
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }

    if (streamError && !finalPayload) {
      throw new Error(streamError);
    }

    if (!finalPayload) {
      throw new Error('Streaming connection closed before receiving final response.');
    }

    return finalPayload;
  }

  async getIncident(incidentId: string): Promise<Record<string, any>> {
    const res = await fetch(`${API_BASE}/incidents/${encodeURIComponent(incidentId)}`);
    return parseResponse(res, `Failed to get incident ${incidentId}`);
  }

  async getAuditTrail(incidentId?: string): Promise<AuditTrailResponse> {
    const url = incidentId
      ? `${API_BASE}/audit?incident_id=${encodeURIComponent(incidentId)}`
      : `${API_BASE}/audit`;
    const res = await fetch(url);
    return parseResponse<AuditTrailResponse>(res, 'Get audit trail failed');
  }
}

export const apiClient = new FinPilotApiClient();
