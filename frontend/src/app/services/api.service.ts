import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import type { Observable } from 'rxjs';
import type {
  ApplicationDetail,
  ApplicationItem,
  AuthStatusReport,
  AutomationStatus,
  CandidateProfile,
  PipelineStatus,
  TrackerRecord,
  TrackerStats,
} from '../models/types';

@Injectable({
  providedIn: 'root',
})
export class ApiService {
  private http = inject(HttpClient);

  getStats(): Observable<TrackerStats> {
    return this.http.get<TrackerStats>('/api/stats');
  }

  getApplications(
    search = '',
    limit = 60,
    offset = 0,
  ): Observable<{ items: ApplicationItem[]; total: number }> {
    let url = `/api/applications?limit=${limit}&offset=${offset}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    return this.http.get<{ items: ApplicationItem[]; total: number }>(url);
  }

  getApplication(appId: string): Observable<ApplicationDetail> {
    return this.http.get<ApplicationDetail>(`/api/applications/${encodeURIComponent(appId)}`);
  }

  updateCoverLetter(
    appId: string,
    coverLetter: string,
  ): Observable<{ status: string; message: string }> {
    return this.http.post<{ status: string; message: string }>(
      `/api/applications/${encodeURIComponent(appId)}/update-cover-letter`,
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
    }>(`/api/applications/${encodeURIComponent(appId)}/regenerate-cv`, {});
  }

  applyForJob(
    appId: string,
    mode: 'assisted' | 'autonomous' = 'assisted',
  ): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(
      `/api/applications/${encodeURIComponent(appId)}/apply`,
      { mode },
    );
  }

  batchApply(
    count = 5,
    mode: 'assisted' | 'autonomous' = 'assisted',
  ): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/batch-apply', { count, mode });
  }

  markDone(appId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(
      `/api/applications/${encodeURIComponent(appId)}/mark-done`,
      {},
    );
  }

  deleteApplication(appId: string): Observable<Record<string, unknown>> {
    return this.http.delete<Record<string, unknown>>(
      `/api/applications/${encodeURIComponent(appId)}`,
    );
  }

  clearFailed(): Observable<{ status: string; message: string; removed_count: number }> {
    return this.http.post<{ status: string; message: string; removed_count: number }>(
      '/api/applications/clear-failed',
      {},
    );
  }

  pruneInactive(): Observable<{ status: string; pruned_count: number; message: string }> {
    return this.http.post<{ status: string; pruned_count: number; message: string }>(
      '/api/applications/prune-inactive',
      {},
    );
  }

  pruneNonEmea(
    region = 'EMEA',
    countries: string[] = [],
  ): Observable<{ status: string; pruned_count: number; message: string }> {
    return this.http.post<{ status: string; pruned_count: number; message: string }>(
      '/api/applications/prune-by-region',
      {
        region,
        countries,
        strict: true,
      },
    );
  }

  pruneDuplicates(): Observable<{ status: string; pruned_count: number; message: string }> {
    return this.http.post<{ status: string; pruned_count: number; message: string }>(
      '/api/applications/prune-duplicates',
      {},
    );
  }

  getTracker(status = '', limit = 150): Observable<{ records: TrackerRecord[]; total: number }> {
    let url = `/api/tracker?limit=${limit}`;
    if (status) url += `&status=${encodeURIComponent(status)}`;
    return this.http.get<{ records: TrackerRecord[]; total: number }>(url);
  }

  updateTrackerStatus(
    jobUrl: string,
    status: string,
    notes = '',
  ): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/tracker/update-status', {
      job_url: jobUrl,
      status,
      notes,
    });
  }

  requeue(jobUrl: string, folderName = ''): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/tracker/requeue', {
      job_url: jobUrl,
      folder_name: folderName,
    });
  }

  getDiagnostics(appId: string): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(
      `/api/applications/${encodeURIComponent(appId)}/diagnostics`,
    );
  }

  getProfile(): Observable<CandidateProfile> {
    return this.http.get<CandidateProfile>('/api/profile');
  }

  updateProfile(profile: Partial<CandidateProfile>): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/profile', profile);
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
    return this.http.post<Record<string, unknown>>('/api/pipeline/run', body);
  }

  getPipelineStatus(): Observable<PipelineStatus> {
    return this.http.get<PipelineStatus>('/api/pipeline/status');
  }

  clearLogs(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/logs/clear', {});
  }

  getAutomationStatus(): Observable<AutomationStatus> {
    return this.http.get<AutomationStatus>('/api/automation/status');
  }

  submitVerificationCode(code: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/automation/submit-code', { code });
  }

  getAuthStatus(): Observable<AuthStatusReport> {
    return this.http.get<AuthStatusReport>('/api/auth/status');
  }

  launchAuthLogin(platform: string, timeout = 180): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/auth/login', { platform, timeout });
  }

  syncDesktopChrome(): Observable<{ status: string; cookies_merged: number; message: string }> {
    return this.http.post<{ status: string; cookies_merged: number; message: string }>(
      '/api/auth/sync-chrome',
      {},
    );
  }

  importBackup(formData: FormData): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>('/api/import', formData);
  }
}
