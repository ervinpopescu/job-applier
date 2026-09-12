import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import type { Observable } from 'rxjs';
import type {
  ApplicationDetail,
  ApplicationItem,
  ApplicationAutomationEventsResponse,
  ApplicationAutomationStatusResponse,
  MainResumeResponse,
  AuthStatusReport,
  AutomationFunnel,
  AutomationStatus,
  CandidateProfile,
  DurableNotification,
  NotificationListResponse,
  PipelineStatus,
  QueueJobActionResponse,
  TakeoverStatus,
  TrackerRecord,
  TrackerStats,
} from '../models/types';

/**
 * Resolves an API or file path against the current document baseURI (e.g. `/` or `/job-applier/`).
 * Preserves external URLs (http/https/blob/data) and already-prefixed paths.
 */
export function resolveApiUrl(path: string, baseURI?: string): string {
  if (!path) return '';

  // Preserve absolute URLs with scheme (http://, https://, blob:, data:, etc.)
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(path)) {
    return path;
  }

  const rawBase =
    baseURI ?? (typeof document !== 'undefined' && document.baseURI ? document.baseURI : '/');

  let basePath = '/';
  try {
    const parsed = new URL(rawBase, 'http://localhost');
    basePath = parsed.pathname;
  } catch {
    basePath = rawBase.startsWith('/') ? rawBase : '/' + rawBase;
  }

  if (!basePath.startsWith('/')) {
    basePath = '/' + basePath;
  }
  if (!basePath.endsWith('/')) {
    basePath += '/';
  }

  const cleanPath = path.startsWith('/') ? path.slice(1) : path;
  const subpathPrefix = basePath.slice(1); // e.g. "job-applier/" or ""
  if (subpathPrefix && cleanPath.startsWith(subpathPrefix)) {
    return '/' + cleanPath;
  }

  return basePath === '/' ? '/' + cleanPath : basePath + cleanPath;
}

export function resolveFileUrl(path: string | null | undefined, baseURI?: string): string {
  if (!path) return '';
  return resolveApiUrl(path, baseURI);
}

@Injectable({
  providedIn: 'root',
})
export class ApiService {
  private http = inject(HttpClient);

  resolveUrl(path: string): string {
    return resolveApiUrl(path);
  }

  getExportUrl(): string {
    return this.resolveUrl('/api/export');
  }

  getStats(): Observable<TrackerStats> {
    return this.http.get<TrackerStats>(this.resolveUrl('/api/stats'));
  }

  getApplications(
    search = '',
    limit = 60,
    offset = 0,
    queueOnly = true,
  ): Observable<{ items: ApplicationItem[]; total: number; scope?: string }> {
    let url = `/api/applications?limit=${limit}&offset=${offset}&queue_only=${queueOnly}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    return this.http.get<{ items: ApplicationItem[]; total: number }>(this.resolveUrl(url));
  }

  getApplication(appId: string): Observable<ApplicationDetail> {
    return this.http.get<ApplicationDetail>(
      this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}`),
    );
  }

  updateCoverLetter(
    appId: string,
    coverLetter: string,
  ): Observable<{ status: string; message: string }> {
    return this.http.post<{ status: string; message: string }>(
      this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}/update-cover-letter`),
      { cover_letter: coverLetter },
    );
  }

  regenerateCv(appId: string): Observable<{
    status: string;
    cv_filename: string;
    cv_pdf_url: string;
    cover_letter: string;
    message: string;
  }> {
    return this.http.post<{
      status: string;
      cv_filename: string;
      cv_pdf_url: string;
      cover_letter: string;
      message: string;
    }>(this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}/regenerate-cv`), {});
  }

  applyForJob(
    appId: string,
    mode: 'assisted' | 'autonomous' = 'assisted',
  ): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(
      this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}/apply`),
      { mode },
    );
  }

  batchApply(
    count = 5,
    mode: 'assisted' | 'autonomous' = 'assisted',
  ): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/batch-apply'), {
      count,
      mode,
    });
  }

  markDone(appId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(
      this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}/mark-done`),
      {},
    );
  }

  deleteApplication(appId: string): Observable<Record<string, unknown>> {
    return this.http.delete<Record<string, unknown>>(
      this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}`),
    );
  }

  clearFailed(): Observable<{ status: string; message: string; removed_count: number }> {
    return this.http.post<{ status: string; message: string; removed_count: number }>(
      this.resolveUrl('/api/applications/clear-failed'),
      {},
    );
  }

  clearAutomationState(): Observable<{
    status: string;
    message: string;
    reset_jobs_count: number;
    browser_closed: boolean;
    is_paused: boolean;
    is_stopped: boolean;
  }> {
    return this.http.post<{
      status: string;
      message: string;
      reset_jobs_count: number;
      browser_closed: boolean;
      is_paused: boolean;
      is_stopped: boolean;
    }>(this.resolveUrl('/api/automation/clear-state'), {});
  }

  pruneInactive(): Observable<{ status: string; pruned_count: number; message: string }> {
    return this.http.post<{ status: string; pruned_count: number; message: string }>(
      this.resolveUrl('/api/applications/prune-inactive'),
      {},
    );
  }

  pruneNonEmea(
    region = 'EMEA',
    countries: string[] = [],
  ): Observable<{ status: string; pruned_count: number; message: string }> {
    return this.http.post<{ status: string; pruned_count: number; message: string }>(
      this.resolveUrl('/api/applications/prune-by-region'),
      {
        region,
        countries,
        strict: true,
      },
    );
  }

  pruneDuplicates(): Observable<{ status: string; pruned_count: number; message: string }> {
    return this.http.post<{ status: string; pruned_count: number; message: string }>(
      this.resolveUrl('/api/applications/prune-duplicates'),
      {},
    );
  }

  getTracker(status = '', limit = 150): Observable<{ records: TrackerRecord[]; total: number }> {
    let url = `/api/tracker?limit=${limit}`;
    if (status) url += `&status=${encodeURIComponent(status)}`;
    return this.http.get<{ records: TrackerRecord[]; total: number }>(this.resolveUrl(url));
  }

  updateTrackerStatus(
    jobUrl: string,
    status: string,
    notes = '',
  ): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/tracker/update-status'), {
      job_url: jobUrl,
      status,
      notes,
    });
  }

  requeue(jobUrl: string, folderName = ''): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/tracker/requeue'), {
      job_url: jobUrl,
      folder_name: folderName,
    });
  }

  batchRequeue(payload: {
    items?: { job_url: string; folder_name: string }[];
    requeue_all?: boolean;
    status_filter?: string;
  }): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(
      this.resolveUrl('/api/tracker/batch-requeue'),
      payload,
    );
  }

  getDiagnostics(appId: string): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(
      this.resolveUrl(`/api/applications/${encodeURIComponent(appId)}/diagnostics`),
    );
  }

  getMainResume(): Observable<MainResumeResponse> {
    return this.http.get<MainResumeResponse>(this.resolveUrl('/api/resume/main'));
  }

  getProfile(): Observable<CandidateProfile> {
    return this.http.get<CandidateProfile>(this.resolveUrl('/api/profile'));
  }

  updateProfile(profile: Partial<CandidateProfile>): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/profile'), profile);
  }

  runPipeline(body: {
    terms: string[];
    locations: string[];
    limit: number;
    sites: string[];
    is_remote?: boolean;
    region?: string;
    countries?: string[];
    auto_apply?: boolean;
    autonomous?: boolean;
  }): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/pipeline/run'), body);
  }

  getPipelineStatus(): Observable<PipelineStatus> {
    return this.http.get<PipelineStatus>(this.resolveUrl('/api/pipeline/status'));
  }

  clearLogs(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/logs/clear'), {});
  }

  getAutomationStatus(): Observable<AutomationStatus> {
    return this.http.get<AutomationStatus>(this.resolveUrl('/api/automation/status'));
  }

  getAutomationFunnel(): Observable<AutomationFunnel> {
    return this.http.get<AutomationFunnel>(this.resolveUrl('/api/automation/funnel'));
  }

  getApplicationAutomationStatus(
    appId: string,
    jobId?: string | null,
  ): Observable<ApplicationAutomationStatusResponse> {
    let url = `/api/automation/applications/${encodeURIComponent(appId)}/status`;
    if (jobId) url += `?job_id=${encodeURIComponent(jobId)}`;
    return this.http.get<ApplicationAutomationStatusResponse>(this.resolveUrl(url));
  }

  getApplicationAutomationEvents(
    appId: string,
    jobId?: string | null,
    after = 0,
    limit = 50,
  ): Observable<ApplicationAutomationEventsResponse> {
    let url = `/api/automation/applications/${encodeURIComponent(appId)}/events?after=${after}&limit=${limit}`;
    if (jobId) url += `&job_id=${encodeURIComponent(jobId)}`;
    return this.http.get<ApplicationAutomationEventsResponse>(this.resolveUrl(url));
  }

  submitVerificationCode(code: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/automation/submit-code'), {
      code,
    });
  }

  getAuthStatus(): Observable<AuthStatusReport> {
    return this.http.get<AuthStatusReport>(this.resolveUrl('/api/auth/status'));
  }

  launchAuthLogin(platform: string, timeout = 180): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/auth/login'), {
      platform,
      timeout,
    });
  }

  syncDesktopChrome(): Observable<{ status: string; cookies_merged: number; message: string }> {
    return this.http.post<{ status: string; cookies_merged: number; message: string }>(
      this.resolveUrl('/api/auth/sync-chrome'),
      {},
    );
  }

  importBackup(formData: FormData): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(this.resolveUrl('/api/import'), formData);
  }

  // --- Durable Notifications ---

  getNotifications(
    after?: number | string,
    unreadOnly = false,
    limit = 50,
  ): Observable<NotificationListResponse> {
    let url = `/api/notifications?limit=${limit}&unread_only=${unreadOnly}`;
    if (after !== undefined && after !== null && after !== '') {
      url += `&after=${encodeURIComponent(after.toString())}`;
    }
    return this.http.get<NotificationListResponse>(this.resolveUrl(url));
  }

  ackNotification(
    notificationId: number | string,
  ): Observable<{ status: string; id: number; notification_id: string; acknowledged: boolean }> {
    return this.http.post<{
      status: string;
      id: number;
      notification_id: string;
      acknowledged: boolean;
    }>(
      this.resolveUrl(`/api/notifications/${encodeURIComponent(notificationId.toString())}/ack`),
      {},
    );
  }

  ackAllNotifications(
    upToId?: number,
  ): Observable<{ status: string; acknowledged_count: number; message: string }> {
    return this.http.post<{ status: string; acknowledged_count: number; message: string }>(
      this.resolveUrl('/api/notifications/ack-all'),
      upToId ? { up_to_id: upToId } : {},
    );
  }

  deleteNotification(
    notificationId: number | string,
  ): Observable<{ status: string; notification_id: string; message: string }> {
    return this.http.delete<{ status: string; notification_id: string; message: string }>(
      this.resolveUrl(`/api/notifications/${encodeURIComponent(notificationId.toString())}`),
    );
  }

  clearAllNotifications(): Observable<{ status: string; cleared_count: number; message: string }> {
    return this.http.delete<{ status: string; cleared_count: number; message: string }>(
      this.resolveUrl('/api/notifications'),
    );
  }

  dispatchNotifications(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(
      this.resolveUrl('/api/notifications/dispatch'),
      {},
    );
  }

  // --- Automation Controls (Pause / Resume / Stop / Queue Actions) ---

  pauseAutomation(): Observable<QueueJobActionResponse> {
    return this.http.post<QueueJobActionResponse>(this.resolveUrl('/api/automation/pause'), {});
  }

  resumeAutomation(): Observable<QueueJobActionResponse> {
    return this.http.post<QueueJobActionResponse>(this.resolveUrl('/api/automation/resume'), {});
  }

  stopAutomation(): Observable<QueueJobActionResponse> {
    return this.http.post<QueueJobActionResponse>(this.resolveUrl('/api/automation/stop'), {});
  }

  cancelJob(jobId: string): Observable<QueueJobActionResponse> {
    return this.http.post<QueueJobActionResponse>(
      this.resolveUrl(`/api/automation/jobs/${encodeURIComponent(jobId)}/cancel`),
      {},
    );
  }

  skipJob(jobId: string): Observable<QueueJobActionResponse> {
    return this.http.post<QueueJobActionResponse>(
      this.resolveUrl(`/api/automation/jobs/${encodeURIComponent(jobId)}/skip`),
      {},
    );
  }

  resolveJob(
    jobId: string,
    resolutionType = 'continue',
    answerValue = '',
    questionKey = '',
    approvedScope = 'global',
  ): Observable<QueueJobActionResponse> {
    return this.http.post<QueueJobActionResponse>(
      this.resolveUrl(`/api/automation/jobs/${encodeURIComponent(jobId)}/resolve`),
      {
        resolution_type: resolutionType,
        answer_value: answerValue,
        question_key: questionKey,
        approved_scope: approvedScope,
      },
    );
  }

  // --- Operator Takeover & noVNC View Controls ---

  getTakeoverStatus(): Observable<TakeoverStatus> {
    return this.http.get<TakeoverStatus>(this.resolveUrl('/api/automation/takeover/status'));
  }

  claimTakeover(
    owner = 'operator',
    leaseSeconds = 300,
  ): Observable<{ status: string; owner: string; expires_at: string; lease_seconds: number }> {
    return this.http.post<{
      status: string;
      owner: string;
      expires_at: string;
      lease_seconds: number;
    }>(this.resolveUrl('/api/automation/takeover/claim'), {
      owner,
      lease_seconds: leaseSeconds,
    });
  }

  releaseTakeover(
    owner = 'operator',
    force = false,
  ): Observable<{ status: string; message: string }> {
    return this.http.post<{ status: string; message: string }>(
      this.resolveUrl('/api/automation/takeover/release'),
      {
        owner,
        force,
      },
    );
  }

  resumeTakeover(jobId?: string): Observable<{ status: string; action: string; message: string }> {
    let url = '/api/automation/takeover/resume';
    if (jobId) {
      url += `?job_id=${encodeURIComponent(jobId)}`;
    }
    return this.http.post<{ status: string; action: string; message: string }>(
      this.resolveUrl(url),
      {},
    );
  }

  reopenAuthSession(
    jobId?: string,
  ): Observable<{ status: string; action: string; message: string }> {
    let url = '/api/automation/takeover/reopen-auth';
    if (jobId) {
      url += `?job_id=${encodeURIComponent(jobId)}`;
    }
    return this.http.post<{ status: string; action: string; message: string }>(
      this.resolveUrl(url),
      {},
    );
  }
}
