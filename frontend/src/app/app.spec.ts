import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { App } from './app';
import { ApiService } from './services/api.service';
import { classifyHttpError } from './models/types';

describe('Error Classification - classifyHttpError', () => {
  it('classifies status 0 as network outage', () => {
    const err = {
      status: 0,
      message: 'Http failure response for http://localhost: 0 Unknown Error',
    };
    const classified = classifyHttpError(err);
    expect(classified.category).toBe('network');
    expect(classified.status).toBe(0);
    expect(classified.title).toBe('Backend Unreachable');
    expect(classified.message).toContain('Cannot connect to the server');
  });

  it('classifies status 401/403 as auth denied', () => {
    const err = { status: 403, error: { detail: 'Forbidden proxy access' } };
    const classified = classifyHttpError(err);
    expect(classified.category).toBe('auth');
    expect(classified.status).toBe(403);
    expect(classified.title).toBe('Access Denied');
    expect(classified.message).toBe('Forbidden proxy access');
  });

  it('classifies status 404 as routing error', () => {
    const err = { status: 404, error: { detail: 'Not Found' } };
    const classified = classifyHttpError(err);
    expect(classified.category).toBe('routing');
    expect(classified.status).toBe(404);
    expect(classified.title).toBe('Endpoint Not Found (404)');
  });

  it('classifies status 500+ as server error', () => {
    const err = { status: 502, error: { detail: 'Bad Gateway' } };
    const classified = classifyHttpError(err);
    expect(classified.category).toBe('server');
    expect(classified.status).toBe(502);
    expect(classified.title).toBe('Server Error (502)');
  });

  it('sanitizes raw HTML, doctype, or tracebacks from error detail case-insensitively', () => {
    const htmlErr = {
      status: 500,
      error: {
        detail: '<!DOCTYPE HTML><html><body>TRACEBACK (most recent call): crash</body></html>',
      },
    };
    const classified = classifyHttpError(htmlErr);
    expect(classified.message).not.toContain('<html');
    expect(classified.message).not.toContain('TRACEBACK');
    expect(classified.message).toBe(
      'An internal server error occurred on the backend. Check service logs.',
    );
  });
});

describe('App Component - State & Degraded Mode Recovery', () => {
  let app: App;
  let mockApi: Partial<ApiService>;

  beforeEach(() => {
    vi.useFakeTimers();

    mockApi = {
      getStats: vi
        .fn()
        .mockReturnValue(of({ pending: 10, applied_folders: 2, tracker: { total_records: 5 } })),
      getApplications: vi
        .fn()
        .mockReturnValue(
          of({ items: [{ id: 'app1', company: 'Acme', title: 'DevOps' }], total: 1 }),
        ),
      getTracker: vi.fn().mockReturnValue(
        of({
          records: [{ timestamp: '2026-09-05', company: 'Acme', status: 'applied' }],
          total: 1,
        }),
      ),
      getProfile: vi
        .fn()
        .mockReturnValue(of({ full_name: 'Alex Example', email: 'alex@example.com' })),
      getAuthStatus: vi
        .fn()
        .mockReturnValue(of({ profile_dir: '.browser_profile', platforms: {} })),
      getPipelineStatus: vi
        .fn()
        .mockReturnValue(of({ is_running: false, status: 'idle', logs: [] })),
      getAutomationStatus: vi.fn().mockReturnValue(of({ is_active: false })),
      importBackup: vi.fn(),
      resolveUrl: vi.fn().mockImplementation((path: string) => path),
      getExportUrl: vi.fn().mockReturnValue('/api/export'),
    };

    TestBed.configureTestingModule({
      imports: [App],
      providers: [{ provide: ApiService, useValue: mockApi }],
    });

    const fixture = TestBed.createComponent(App);
    app = fixture.componentInstance;
  });

  afterEach(() => {
    app.ngOnDestroy();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('loadInitialData settles isLoading to false only after requests complete', async () => {
    expect(app.isLoading()).toBe(false);

    const promise = app.loadInitialData();
    // While in progress or immediately after forkJoin settlement
    await promise;

    expect(app.isLoading()).toBe(false);
    expect(app.stats()?.pending).toBe(10);
    expect(app.applications().length).toBe(1);
    expect(app.resourceStates().applications.status).toBe('ready');
    expect(app.hasDegradedResources()).toBe(false);
  });

  it('handles partial failure: preserves successful data while marking failed resource as degraded', async () => {
    // Stats succeeds, applications fails with 500
    mockApi.getApplications = vi
      .fn()
      .mockReturnValue(
        throwError(() => ({ status: 500, error: { detail: 'DB connection error' } })),
      );

    await app.loadInitialData();

    expect(app.isLoading()).toBe(false);
    // Successful stats is preserved
    expect(app.stats()?.pending).toBe(10);
    expect(app.resourceStates().stats.status).toBe('ready');

    // Failed applications enters error state
    expect(app.resourceStates().applications.status).toBe('error');
    expect(app.resourceStates().applications.error?.category).toBe('server');

    // Overall degraded mode is triggered
    expect(app.hasDegradedResources()).toBe(true);
    expect(app.degradedResourceNames()).toContain('Queue');
    expect(app.primaryDegradedMessage()).toBe('DB connection error');
  });

  it('preserves cached data when a subsequent refresh fails (stale data preservation)', async () => {
    // Initial successful load
    await app.loadInitialData();
    expect(app.applications().length).toBe(1);

    // Subsequent loadApplications fails with status 0
    mockApi.getApplications = vi.fn().mockReturnValue(throwError(() => ({ status: 0 })));
    app.loadApplications();

    // The applications array retains previously loaded data!
    expect(app.applications().length).toBe(1);
    expect(app.resourceStates().applications.status).toBe('error');
    expect(app.hasDegradedResources()).toBe(true);
  });

  it('retryFailedResources retries degraded items and clears degraded status on recovery', async () => {
    mockApi.getApplications = vi.fn().mockReturnValue(throwError(() => ({ status: 503 })));
    await app.loadInitialData();

    expect(app.hasDegradedResources()).toBe(true);
    expect(app.resourceStates().applications.status).toBe('error');

    // Now backend recovers
    mockApi.getApplications = vi
      .fn()
      .mockReturnValue(of({ items: [{ id: 'app1' }, { id: 'app2' }], total: 2 }));

    await app.retryFailedResources();

    expect(app.resourceStates().applications.status).toBe('ready');
    expect(app.hasDegradedResources()).toBe(false);
    expect(app.applications().length).toBe(2);
    expect(app.retryAttempt()).toBe(1);
  });

  it('advances retry failures from the 5-second delay to the 15-second delay', async () => {
    mockApi.getApplications = vi.fn().mockReturnValue(throwError(() => ({ status: 503 })));
    await app.loadInitialData();

    expect(app.retryCountdown()).toBe(5);
    await app.retryFailedResources();

    expect(app.retryAttempt()).toBe(2);
    expect(app.retryCountdown()).toBe(15);
  });

  it('refreshPoll suppresses error toasts to prevent toast storms on background poll failure', () => {
    mockApi.getPipelineStatus = vi
      .fn()
      .mockReturnValue(
        throwError(() => ({ status: 500, error: { detail: 'Internal Server Error' } })),
      );
    mockApi.getAutomationStatus = vi.fn().mockReturnValue(throwError(() => ({ status: 0 })));

    app.refreshPoll();

    // Error states are captured
    expect(app.resourceStates().pipeline.status).toBe('error');
    expect(app.resourceStates().automation.status).toBe('error');

    // Toast remains not visible: no noisy alerts for background polling!
    expect(app.toast().visible).toBe(false);
  });

  it('does not bypass retry backoff by polling failed statistics', () => {
    app.setResourceStatus('stats', 'error', classifyHttpError({ status: 503 }));
    vi.mocked(mockApi.getStats!).mockClear();

    app.refreshPoll();

    expect(mockApi.getStats).not.toHaveBeenCalled();
  });

  it('retries and recovers failed pipeline polling through the shared backoff', async () => {
    mockApi.getPipelineStatus = vi.fn().mockReturnValue(throwError(() => ({ status: 503 })));
    app.refreshPoll();

    expect(app.resourceStates().pipeline.error?.category).toBe('server');
    expect(app.hasDegradedResources()).toBe(true);

    mockApi.getPipelineStatus = vi
      .fn()
      .mockReturnValue(of({ is_running: false, status: 'idle', started_at: '', logs: [] }));
    await app.retryFailedResources();

    expect(app.resourceStates().pipeline.status).toBe('ready');
    expect(app.resourceStates().pipeline.error).toBeNull();
    expect(app.hasDegradedResources()).toBe(false);
  });

  describe('Import Backup UX', () => {
    it('handles import success: clears filters, sets tab to queue, shows count toast', async () => {
      app.searchQuery.set('senior');
      app.trackerFilter.set('applied');

      const mockReport = {
        status: 'success',
        report: {
          imported_applications: 5,
          merged_db_records: 12,
          imported_applied: 2,
        },
      };
      mockApi.importBackup = vi.fn().mockReturnValue(of(mockReport));

      const fakeFile = new File(['mock'], 'backup.zip', { type: 'application/zip' });
      const mockEvent = { target: { files: [fakeFile], value: 'backup.zip' } } as unknown as Event;

      await app.uploadBackup(mockEvent);

      expect(app.isImporting()).toBe(false);
      expect(app.searchQuery()).toBe('');
      expect(app.trackerFilter()).toBe('');
      expect(app.activeTab()).toBe('queue');
      expect(app.toast().visible).toBe(true);
      expect(app.toast().type).toBe('success');
      expect(app.toast().message).toContain('5 applications and 12 records');
    });

    it('warns when import contains zero applications and zero records', async () => {
      const emptyReport = {
        status: 'success',
        report: {
          imported_applications: 0,
          merged_db_records: 0,
          imported_applied: 0,
        },
      };
      mockApi.importBackup = vi.fn().mockReturnValue(of(emptyReport));

      const fakeFile = new File(['mock'], 'empty.zip', { type: 'application/zip' });
      const mockEvent = { target: { files: [fakeFile], value: 'empty.zip' } } as unknown as Event;

      await app.uploadBackup(mockEvent);

      expect(app.toast().type).toBe('warning');
      expect(app.toast().message).toContain('0 applications or records');
    });

    it('handles import failure cleanly: shows sanitized error toast and resets isImporting', async () => {
      mockApi.importBackup = vi
        .fn()
        .mockReturnValue(
          throwError(() => ({ status: 400, error: { detail: 'Corrupted zip file' } })),
        );

      const fakeFile = new File(['mock'], 'bad.zip', { type: 'application/zip' });
      const mockEvent = { target: { files: [fakeFile], value: 'bad.zip' } } as unknown as Event;

      await app.uploadBackup(mockEvent);

      expect(app.isImporting()).toBe(false);
      expect(app.toast().visible).toBe(true);
      expect(app.toast().type).toBe('error');
      expect(app.toast().message).toBe('Corrupted zip file');
    });

    it('reports import-success-but-refresh-failed with warning directing user to retry', async () => {
      const mockReport = {
        status: 'success',
        report: { imported_applications: 4, merged_db_records: 4 },
      };
      mockApi.importBackup = vi.fn().mockReturnValue(of(mockReport));

      mockApi.getApplications = vi
        .fn()
        .mockReturnValue(throwError(() => ({ status: 500, error: { detail: 'Database down' } })));

      const fakeFile = new File(['mock'], 'apps.zip', { type: 'application/zip' });
      const mockEvent = { target: { files: [fakeFile], value: 'apps.zip' } } as unknown as Event;

      await app.uploadBackup(mockEvent);

      expect(app.isImporting()).toBe(false);
      expect(app.resourceStates().applications.status).toBe('error');
      expect(app.toast().type).toBe('warning');
      expect(app.toast().message).toContain('dashboard refresh failed');
    });
  });
});
