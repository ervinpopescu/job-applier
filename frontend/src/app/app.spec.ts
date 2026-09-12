import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
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
  let fixture: ComponentFixture<App>;
  let mockApi: Partial<ApiService>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'EventSource',
      class {
        addEventListener(): void {}
        close(): void {}
      },
    );

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
      getTakeoverStatus: vi.fn().mockReturnValue(of({ is_takeover_active: false })),
      importBackup: vi.fn(),
      resolveUrl: vi.fn().mockImplementation((path: string) => path),
      getExportUrl: vi.fn().mockReturnValue('/api/export'),
      getTrackerExportCsvUrl: vi.fn().mockReturnValue('/api/tracker/export'),
    };

    TestBed.configureTestingModule({
      imports: [App],
      providers: [{ provide: ApiService, useValue: mockApi }],
    });

    fixture = TestBed.createComponent(App);
    app = fixture.componentInstance;
  });

  afterEach(() => {
    app.ngOnDestroy();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('keeps every primary navigation destination visible without a scrolling strip', () => {
    fixture.detectChanges();

    const nav = fixture.nativeElement.querySelector('.primary-nav') as HTMLElement;
    const tabs = Array.from(nav.querySelectorAll('button')) as HTMLButtonElement[];

    expect(tabs).toHaveLength(5);
    expect(nav.className).not.toContain('overflow-x-auto');
    expect(tabs.every((tab) => tab.className.includes('flex-1'))).toBe(true);
    expect(tabs.map((tab) => tab.getAttribute('aria-label'))).toEqual([
      'Applications dashboard',
      'Automation metrics',
      'Scraper configuration',
      'Execution console',
      'Candidate profile',
    ]);
  });

  it('opens the safe main resume viewer from the More actions menu', () => {
    mockApi.getMainResume = vi.fn().mockReturnValue(
      of({
        status: 'ready',
        resume: {
          contact: { name: 'Synthetic Candidate', email: 'candidate@example.test' },
          summary: 'Synthetic summary',
          experience: [],
          skills: ['Python'],
          education: {},
          projects: [
            { name: 'Example', description: 'Example project', url: 'https://example.test' },
          ],
        },
        artifact_url: '/api/resume/main.pdf',
        generation_id: null,
        updated_at: null,
        error: null,
        retry_after_seconds: null,
      }),
    );

    app.openResumeViewer();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('[aria-labelledby="resume-viewer-title"]'),
    ).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Synthetic Candidate');
    expect(fixture.nativeElement.textContent).toContain('Example project');
  });

  it('provides distinct, non-duplicated controls for viewing structured resume, previewing PDF, and downloading artifact', () => {
    mockApi.getMainResume = vi.fn().mockReturnValue(
      of({
        status: 'ready',
        resume: {
          contact: { name: 'Synthetic Candidate', email: 'candidate@example.test' },
          summary: 'Synthetic summary',
          experience: [],
          skills: ['Python'],
          education: {},
          projects: [
            { name: 'Example', description: 'Example project', url: 'https://example.test' },
          ],
        },
        artifact_url: '/api/resume/main.pdf',
        download_url: '/api/resume/main.pdf?download=true',
        generation_id: null,
        updated_at: null,
        error: null,
        retry_after_seconds: null,
      }),
    );

    app.openResumeViewer();
    fixture.detectChanges();

    // 1. In structured view: mode tabs and exactly one download action in the header
    const tablist = fixture.nativeElement.querySelector(
      'div[role="tablist"][aria-label="Resume view modes"]',
    ) as HTMLElement;
    expect(tablist).not.toBeNull();

    const tabs = Array.from(tablist.querySelectorAll('button[role="tab"]')) as HTMLButtonElement[];
    expect(tabs.length).toBe(2);
    const [structuredTab, previewTab] = tabs;

    expect(structuredTab.textContent?.trim()).toContain('Structured');
    expect(structuredTab.getAttribute('aria-selected')).toBe('true');
    expect(previewTab.textContent?.trim()).toContain('PDF Preview');
    expect(previewTab.getAttribute('aria-selected')).toBe('false');

    // Exactly one persistent Download PDF link exists in the viewer (in header)
    const downloadLinks = fixture.nativeElement.querySelectorAll(
      'a[aria-label="Download main resume PDF"]',
    );
    expect(downloadLinks.length).toBe(1);
    const downloadLink = downloadLinks[0] as HTMLAnchorElement;
    expect(downloadLink.getAttribute('download')).toBe('main-resume.pdf');
    expect(downloadLink.getAttribute('href')).toContain('/api/resume/main.pdf?download=true');

    // Structured resume content is visible
    expect(fixture.nativeElement.textContent).toContain('Synthetic Candidate');

    // Verify duplicate controls are absent: no duplicate Open PDF button
    expect(
      fixture.nativeElement.querySelector('button[aria-label="Open PDF inline preview"]'),
    ).toBeNull();

    // 2. Switch to PDF preview via the mode switcher tab
    previewTab.click();
    fixture.detectChanges();

    expect(structuredTab.getAttribute('aria-selected')).toBe('false');
    expect(previewTab.getAttribute('aria-selected')).toBe('true');

    // PDF preview iframe is displayed
    const iframe = fixture.nativeElement.querySelector(
      'iframe[title="Main resume PDF inline preview"]',
    ) as HTMLIFrameElement;
    expect(iframe).not.toBeNull();
    expect(iframe.getAttribute('src')).toContain('/api/resume/main.pdf');

    // In preview mode: Open in new tab is available
    const openInNewTabLink = fixture.nativeElement.querySelector(
      'a[aria-label="Open PDF preview in new tab"]',
    ) as HTMLAnchorElement;
    expect(openInNewTabLink).not.toBeNull();
    expect(openInNewTabLink.getAttribute('href')).toContain('/api/resume/main.pdf');
    expect(openInNewTabLink.getAttribute('target')).toBe('_blank');

    // Verify duplicate controls are absent in preview mode:
    // No duplicate Back to structured view button (switching is owned by the header tablist)
    expect(
      fixture.nativeElement.querySelector('button[aria-label="Back to structured resume view"]'),
    ).toBeNull();
    // No duplicate Download PDF link in preview toolbar (still exactly one in header)
    expect(
      fixture.nativeElement.querySelectorAll('a[aria-label="Download main resume PDF"]').length,
    ).toBe(1);

    // 3. Switch back to structured view via the mode switcher tab
    structuredTab.click();
    fixture.detectChanges();

    expect(structuredTab.getAttribute('aria-selected')).toBe('true');
    expect(previewTab.getAttribute('aria-selected')).toBe('false');
    expect(
      fixture.nativeElement.querySelector('iframe[title="Main resume PDF inline preview"]'),
    ).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Synthetic Candidate');

    // 4. Close the viewer via the dedicated close button
    const closeBtn = fixture.nativeElement.querySelector(
      'button[aria-label="Close main resume viewer"]',
    ) as HTMLButtonElement;
    expect(closeBtn).not.toBeNull();
    closeBtn.click();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('[aria-labelledby="resume-viewer-title"]'),
    ).toBeNull();
  });

  it('renders resume viewer buttons with matching Lucide file-text icon, accessible labeling, and correct spacing', async () => {
    await app.loadInitialData();
    fixture.detectChanges();

    // Primary Review CV viewer button in queue card actions
    const reviewBtn = fixture.nativeElement.querySelector(
      'button[aria-label="Review tailored CV"]',
    ) as HTMLButtonElement;
    expect(reviewBtn).not.toBeNull();
    expect(reviewBtn.getAttribute('title')).toBe('Review tailored CV');
    expect(reviewBtn.className).toContain('flex');
    expect(reviewBtn.className).toContain('items-center');
    expect(reviewBtn.className).toContain('gap-1');

    const reviewIcon = reviewBtn.querySelector('app-icon');
    expect(reviewIcon).not.toBeNull();
    expect(reviewIcon?.getAttribute('name')).toBe('file-text');
    expect(reviewIcon?.querySelector('svg g')).not.toBeNull();

    // Main resume viewer button in more actions menu
    const moreButton = fixture.nativeElement.querySelector(
      '.more-actions-trigger',
    ) as HTMLButtonElement;
    moreButton.click();
    fixture.detectChanges();

    const mainResumeBtn = fixture.nativeElement.querySelector(
      'button[aria-label="View main resume"]',
    ) as HTMLButtonElement;
    expect(mainResumeBtn).not.toBeNull();
    expect(mainResumeBtn.getAttribute('title')).toBe('View the canonical main resume');
    expect(mainResumeBtn.className).toContain('flex');
    expect(mainResumeBtn.className).toContain('items-center');
    expect(mainResumeBtn.className).toContain('gap-2');

    const mainResumeIcon = mainResumeBtn.querySelector('app-icon');
    expect(mainResumeIcon).not.toBeNull();
    expect(mainResumeIcon?.getAttribute('name')).toBe('file-text');
    expect(mainResumeIcon?.querySelector('svg g')).not.toBeNull();
  });

  it('keeps compact actions accessible through the More actions menu', async () => {
    fixture.detectChanges();

    const shell = fixture.nativeElement.querySelector('.app-shell') as HTMLElement;
    const moreButton = fixture.nativeElement.querySelector(
      '.more-actions-trigger',
    ) as HTMLButtonElement;
    const actionButtons = Array.from(
      fixture.nativeElement.querySelectorAll('.header-actions > button'),
    ) as HTMLButtonElement[];

    expect(shell.className).not.toContain('overflow-x-hidden');
    expect(actionButtons.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Manage platform logins and session cookies',
      'Open live browser view and operator takeover controls',
      'Open alerts and notification history',
    ]);
    expect(moreButton.getAttribute('aria-label')).toBe('More actions');
    expect(moreButton.getAttribute('aria-expanded')).toBe('false');
    expect(fixture.nativeElement.querySelector('#more-actions-menu')).toBeNull();

    moreButton.click();
    fixture.detectChanges();

    expect(moreButton.getAttribute('aria-expanded')).toBe('true');
    const menu = fixture.nativeElement.querySelector('#more-actions-menu') as HTMLElement;
    expect(menu.getAttribute('role')).toBe('menu');
    expect(
      Array.from(menu.querySelectorAll('[role="menuitem"]')).map((item) =>
        item.textContent?.trim(),
      ),
    ).toEqual(['Export backup', 'Import backup', 'View main resume']);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    fixture.detectChanges();
    await Promise.resolve();
    expect(moreButton.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(moreButton);

    moreButton.click();
    fixture.detectChanges();
    document.body.click();
    fixture.detectChanges();
    await Promise.resolve();
    expect(moreButton.getAttribute('aria-expanded')).toBe('false');
  });

  it('distinguishes authentication waiting from a true emergency stop', () => {
    app.automationStatus.set({
      is_active: false,
      step: 'auth_required',
      message: 'Authentication required.',
    });
    app.notifService.isPaused.set(true);
    app.notifService.isStopped.set(false);

    expect(app.isAuthenticationWaiting()).toBe(true);
    expect(app.automationBannerTitle()).toBe('Waiting for Authentication');
    expect(app.automationBannerMessage()).toContain('Open Browser View');

    app.notifService.isStopped.set(true);
    expect(app.automationBannerTitle()).toBe('Automation Stopped (Emergency Stop)');
    expect(app.automationBannerMessage()).toContain('Emergency stop engaged');
  });

  it('renders durable funnel metrics and sanitized per-application history state', () => {
    mockApi.getAutomationFunnel = vi.fn().mockReturnValue(
      of({
        verified_autonomous_submissions: 1,
        jobs_tracked: 4,
        runnable_jobs: 0,
        jobs_queued: 4,
        jobs_with_attempt: 4,
        current_auth_blocked_jobs: 1,
        current_site_changed_jobs: 2,
        historical_auth_blocked_attempts: 1,
        historical_site_changed_attempts: 2,
        total_attempts: 4,
        current_permanent_failures: 0,
        current_retry_wait_jobs: 1,
        current_skipped_jobs: 0,
        generated_artifacts: 7,
        manual_applied: 2,
        applied_artifacts: 2,
        unqueued_artifacts: 3,
        jobs_attempted: 4,
        auth_required: 1,
        site_changed: 2,
        permanent_failures: 0,
        retries: 1,
        unknown: 0,
        jobs_total: 4,
        attempts_total: 4,
        deprecated_fields: ['jobs_attempted', 'auth_required', 'site_changed'],
        job_states: {},
        attempt_outcomes: {},
      }),
    );
    mockApi.getApplicationAutomationStatus = vi.fn().mockReturnValue(
      of({
        app_id: 'app1',
        jobs: [{ job_id: 'job1', app_id: 'app1', state: 'in_progress', step: 'filling' }],
      }),
    );
    mockApi.getApplicationAutomationEvents = vi.fn().mockReturnValue(
      of({
        app_id: 'app1',
        events: [
          {
            id: 1,
            app_id: 'app1',
            event_type: 'filling',
            message: 'Sanitized event',
            created_at: '2026-09-09T12:00:00Z',
          },
        ],
        next_after: 1,
        has_more: false,
      }),
    );

    app.loadAutomationFunnel();
    fixture.detectChanges();
    const funnelText = fixture.nativeElement.textContent as string;
    expect(funnelText).toContain('Jobs tracked');
    expect(funnelText).toContain('Runnable jobs');
    expect(funnelText).toContain('Auth-block attempts (historical)');
    expect(funnelText).toContain('Attempt rows (all)');
    expect(funnelText).not.toContain('Jobs attempted');

    app.toggleAutomationHistory({
      id: 'app1',
      company: 'Acme',
      title: 'DevOps',
      job_url: 'https://example.test',
      cv_filename: '',
      has_cover_letter: false,
      cover_letter_preview: '',
      pdf_url: '',
      created_at: '',
    });

    expect(app.automationFunnel()?.verified_autonomous_submissions).toBe(1);
    expect(app.isAutomationHistoryOpen('app1')).toBe(true);
    expect(app.automationHistory()['app1'].events[0].message).toBe('Sanitized event');
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
      expect(app.activeTab()).toBe('applications');
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

    it('computes vncUrl pointing to vnc_lite.html with scale and path=browser/websockify', () => {
      const urlStr = String(app.vncUrl());
      expect(urlStr).toContain('/browser/vnc_lite.html?scale=true&path=browser/websockify');
    });
  });

  describe('Unified Applications Dashboard', () => {
    it('switches status filter pills and loads filtered applications', () => {
      mockApi.getApplications = vi.fn().mockReturnValue(
        of({
          items: [
            {
              id: 'app-queued-1',
              company: 'Stripe',
              title: 'Infrastructure Engineer',
              job_url: 'https://stripe.com/jobs/1',
              cv_filename: 'cv.pdf',
              has_cover_letter: true,
              cover_letter_preview: 'Preview',
              pdf_url: '/files/cv.pdf',
              created_at: '2026-09-12',
              status: 'pending',
              job_state: 'ready',
              job_id: 'job-1',
            },
          ],
          total: 1,
          counts: { all: 10, queued: 1, action_required: 0, pending: 8, applied: 1, skipped: 0 },
        }),
      );

      app.setAppFilter('queued');
      expect(app.appFilter()).toBe('queued');
      expect(mockApi.getApplications).toHaveBeenCalledWith('', 100, 0, undefined, 'queued');
      expect(app.applications()).toHaveLength(1);
      expect(app.appFilterCounts().all).toBe(10);
      expect(app.appFilterCounts().queued).toBe(1);
    });

    it('toggles multi-selection and executes bulk enqueue', () => {
      app.applications.set([
        {
          id: 'app-1',
          company: 'GitHub',
          title: 'Staff Engineer',
          job_url: 'https://github.com/jobs/1',
          cv_filename: '',
          has_cover_letter: false,
          cover_letter_preview: '',
          pdf_url: '',
          created_at: '2026-09-12',
        },
        {
          id: 'app-2',
          company: 'Vercel',
          title: 'Frontend Engineer',
          job_url: 'https://vercel.com/jobs/2',
          cv_filename: '',
          has_cover_letter: false,
          cover_letter_preview: '',
          pdf_url: '',
          created_at: '2026-09-12',
        },
      ]);

      expect(app.isAllAppsSelected()).toBe(false);
      app.toggleSelectAllApps();
      expect(app.isAllAppsSelected()).toBe(true);
      expect(app.selectedAppCount()).toBe(2);

      mockApi.batchApply = vi.fn().mockReturnValue(of({ status: 'queued', queued_count: 2 }));
      app.queueSelectedApps();
      expect(mockApi.batchApply).toHaveBeenCalledWith(2, 'assisted', ['app-1', 'app-2']);
      expect(app.selectedAppCount()).toBe(0);
      expect(app.toast().visible).toBe(true);
      expect(app.toast().message).toContain('Enqueued 2 selected applications');
    });

    it('toggles queue status for a single application', () => {
      mockApi.cancelJob = vi.fn().mockReturnValue(of({ status: 'cancelled' }));
      const queuedApp = {
        id: 'app-queued',
        company: 'Figma',
        title: 'Design Technologist',
        job_url: 'https://figma.com/jobs/1',
        cv_filename: '',
        has_cover_letter: false,
        cover_letter_preview: '',
        pdf_url: '',
        created_at: '2026-09-12',
        job_id: 'job-figma',
        job_state: 'ready',
      };

      app.toggleQueueApp(queuedApp);
      expect(mockApi.cancelJob).toHaveBeenCalledWith('job-figma');
      expect(app.toast().message).toContain('Cancelled queue job for Figma');

      mockApi.batchApply = vi.fn().mockReturnValue(of({ status: 'queued' }));
      const unqueuedApp = {
        id: 'app-unqueued',
        company: 'Canva',
        title: 'Web Engineer',
        job_url: 'https://canva.com/jobs/2',
        cv_filename: '',
        has_cover_letter: false,
        cover_letter_preview: '',
        pdf_url: '',
        created_at: '2026-09-12',
      };
      app.toggleQueueApp(unqueuedApp);
      expect(mockApi.batchApply).toHaveBeenCalledWith(1, 'assisted', ['app-unqueued']);
      expect(app.toast().message).toContain('Enqueued Canva');
    });

    it('opens takeover modal when action is required and stores activeTakeoverJobId', () => {
      mockApi.claimTakeover = vi
        .fn()
        .mockReturnValue(of({ status: 'claimed', lease_seconds: 300 }));
      mockApi.reopenAuthSession = vi
        .fn()
        .mockReturnValue(of({ status: 'ok', message: 'Reopened' }));
      mockApi.resumeTakeover = vi.fn().mockReturnValue(of({ status: 'ok', message: 'Resumed' }));

      app.openTakeoverForJob('job-action-needed');
      expect(mockApi.claimTakeover).toHaveBeenCalled();
      expect(app.vncModalOpen()).toBe(true);
      expect(app.activeTakeoverJobId()).toBe('job-action-needed');

      app.reopenAuthSession();
      expect(mockApi.reopenAuthSession).toHaveBeenCalledWith('job-action-needed');

      app.openTakeoverForJob('job-action-needed');
      app.resumeFromTakeover();
      expect(mockApi.resumeTakeover).toHaveBeenCalledWith('job-action-needed');

      app.closeTakeoverModal();
      expect(app.activeTakeoverJobId()).toBeNull();
    });

    it('evaluates isAppCancellable accurately across queue states', () => {
      const activeApp = {
        id: 'app-active',
        company: 'Stripe',
        title: 'API Engineer',
        job_url: 'https://stripe.com/jobs/1',
        cv_filename: '',
        has_cover_letter: false,
        cover_letter_preview: '',
        pdf_url: '',
        created_at: '2026-09-12',
        job_id: 'job-stripe',
        job_state: 'filling',
      };
      expect(app.isAppCancellable(activeApp)).toBe(true);

      const readyApp = { ...activeApp, job_state: 'ready' };
      expect(app.isAppCancellable(readyApp)).toBe(true);

      const completedApp = { ...activeApp, job_state: 'completed' };
      expect(app.isAppCancellable(completedApp)).toBe(false);

      const unqueuedApp = { ...activeApp, job_id: undefined, job_state: undefined };
      expect(app.isAppCancellable(unqueuedApp)).toBe(false);
    });

    it('clears selectedAppIds when switching app filter', () => {
      app.selectedAppIds.set(new Set(['app-1', 'app-2']));
      expect(app.selectedAppCount()).toBe(2);

      app.setAppFilter('applied');
      expect(app.appFilter()).toBe('applied');
      expect(app.selectedAppCount()).toBe(0);
      expect(app.selectedAppIds().size).toBe(0);
    });

    it('displays count when queueAllPending succeeds with count response', () => {
      mockApi.batchRequeue = vi.fn().mockReturnValue(of({ success: true, count: 5 }));
      app.queueAllPending();
      expect(mockApi.batchRequeue).toHaveBeenCalledWith({
        requeue_all: true,
        status_filter: 'pending',
      });
      expect(app.toast().visible).toBe(true);
      expect(app.toast().message).toContain('Enqueued 5 pending applications!');
    });

    it('triggers export CSV URL download', () => {
      const clickSpy = vi.fn();
      const mockAnchor = {
        href: '',
        setAttribute: vi.fn(),
        click: clickSpy,
      } as unknown as HTMLAnchorElement;

      vi.spyOn(document, 'createElement').mockReturnValue(mockAnchor);
      vi.spyOn(document.body, 'appendChild').mockImplementation(() => mockAnchor);
      vi.spyOn(document.body, 'removeChild').mockImplementation(() => mockAnchor);

      app.exportCsv();
      expect(mockAnchor.setAttribute).toHaveBeenCalledWith('download', 'applications_tracker.csv');
      expect(clickSpy).toHaveBeenCalled();
    });
  });
});
