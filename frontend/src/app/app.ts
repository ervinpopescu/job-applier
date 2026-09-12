import {
  Component,
  type ElementRef,
  type OnDestroy,
  type OnInit,
  HostListener,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, type SafeResourceUrl } from '@angular/platform-browser';
import { type Observable, forkJoin, of } from 'rxjs';
import { catchError, tap } from 'rxjs/operators';
import { IconComponent } from './components/icon.component';
import { ApiService, resolveFileUrl } from './services/api.service';
import {
  type ApplicationDetail,
  type ApplicationItem,
  type MainResume,
  type MainResumeResponse,
  type AuthStatusReport,
  type AutomationEvent,
  type AutomationFunnel,
  type AutomationJobStatus,
  type AutomationStatus,
  type CandidateProfile,
  type ClassifiedError,
  type PipelineStatus,
  type ResourceKey,
  type ResourceState,
  type ResourceStateStatus,
  type TakeoverStatus,
  type ToastNotification,
  type TrackerRecord,
  type TrackerStats,
  classifyHttpError,
} from './models/types';
import { NotificationService } from './services/notification.service';

const RETRY_BACKOFF_STEPS = [5, 15, 30];

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App implements OnInit, OnDestroy {
  api = inject(ApiService);
  notifService = inject(NotificationService);
  private sanitizer = inject(DomSanitizer);

  @ViewChild('consoleContainer') consoleContainer?: ElementRef<HTMLDivElement>;
  @ViewChild('moreActionsButton') moreActionsButton?: ElementRef<HTMLButtonElement>;

  // Tabs
  activeTab = signal<'queue' | 'tracker' | 'scraper' | 'console' | 'profile'>('queue');

  // Core Data Signals
  stats = signal<TrackerStats | null>(null);
  automationFunnel = signal<AutomationFunnel | null>(null);
  applications = signal<ApplicationItem[]>([]);
  selectedApp = signal<ApplicationDetail | null>(null);
  trackerRecords = signal<TrackerRecord[]>([]);
  pipelineStatus = signal<PipelineStatus | null>(null);
  automationStatus = signal<AutomationStatus | null>(null);
  profile = signal<CandidateProfile | null>(null);
  authStatus = signal<AuthStatusReport | null>(null);

  // Takeover & noVNC Viewer State
  vncModalOpen = signal(false);
  takeoverStatus = signal<TakeoverStatus | null>(null);
  takeoverCountdown = signal(300);
  isClaimingTakeover = signal(false);
  isReleasingTakeover = signal(false);
  isResumingTakeover = signal(false);
  isReopeningAuth = signal(false);
  private takeoverTimerId: ReturnType<typeof setInterval> | null = null;

  // Queue & Automation Runtime Controls
  isPausingAutomation = signal(false);
  isResumingAutomation = signal(false);
  isStoppingAutomation = signal(false);
  isClearingAutomationState = signal(false);

  // Responsive overflow menu
  moreActionsOpen = signal(false);

  // Per-application durable automation history. Event messages are sanitized by the API;
  // the UI renders only plain text fields and never interprets event details as HTML.
  automationHistoryOpen = signal<Record<string, boolean>>({});
  automationHistory = signal<
    Record<
      string,
      {
        jobs: AutomationJobStatus[];
        events: AutomationEvent[];
        nextAfter: number;
        hasMore: boolean;
        loading: boolean;
        error: string | null;
      }
    >
  >({});

  // Main resume viewer
  resumeModalOpen = signal(false);
  mainResume = signal<MainResume | null>(null);
  resumeViewStatus = signal<'idle' | 'loading' | 'stale' | 'generating' | 'ready' | 'error'>(
    'idle',
  );
  resumeArtifactUrl = signal<string | null>(null);
  resumeArtifactDownloadUrl = signal<string | null>(null);
  resumeViewerMode = signal<'structured' | 'preview'>('structured');
  resumeViewError = signal<string | null>(null);
  private resumePollTimerId: ReturnType<typeof setInterval> | null = null;
  private resumePollAttempts = 0;

  // Notifications Drawer & Settings Modal
  notificationsDrawerOpen = signal(false);
  notifSettingsModalOpen = signal(false);

  // Exception Resolution Modal
  resolveModalOpen = signal(false);
  resolveJobId = signal('');
  resolveQuestionKey = signal('');
  resolveAnswerValue = signal('');
  resolveApprovedScope = signal<string>('global');
  isResolvingJob = signal(false);

  vncUrl = computed(() =>
    this.sanitizer.bypassSecurityTrustResourceUrl(
      this.api.resolveUrl('/browser/vnc_lite.html?scale=true&path=browser/websockify'),
    ),
  );

  // Per-Resource State Tracking
  resourceStates = signal<Record<ResourceKey, ResourceState>>({
    stats: { status: 'idle', error: null, lastSuccess: null },
    applications: { status: 'idle', error: null, lastSuccess: null },
    tracker: { status: 'idle', error: null, lastSuccess: null },
    profile: { status: 'idle', error: null, lastSuccess: null },
    auth: { status: 'idle', error: null, lastSuccess: null },
    pipeline: { status: 'idle', error: null, lastSuccess: null },
    automation: { status: 'idle', error: null, lastSuccess: null },
  });

  // Degraded Mode & Recovery
  retryAttempt = signal(1);
  retryCountdown = signal(5);
  isRetrying = signal(false);
  isImporting = signal(false);

  private pollIntervalId: ReturnType<typeof setInterval> | null = null;
  private countdownTimerId: ReturnType<typeof setInterval> | null = null;

  // Modals & Banners
  authModalOpen = signal(false);
  diagModalOpen = signal(false);
  diagData = signal<Record<string, unknown> | null>(null);
  toast = signal<ToastNotification>({
    visible: false,
    message: '',
    type: 'success',
  });

  // Filters & State
  searchQuery = signal('');
  trackerFilter = signal('');
  sortColumn = signal<string>('timestamp');
  sortAsc = signal(false);
  verificationCode = signal('');

  // Tracker Multi-Selection
  selectedTrackerUrls = signal<Set<string>>(new Set());
  selectedTrackerCount = computed(() => this.selectedTrackerUrls().size);
  isAllTrackerSelected = computed(() => {
    const records = this.filteredTrackerRecords();
    if (records.length === 0) return false;
    const selected = this.selectedTrackerUrls();
    return records.every((r) => r.job_url && selected.has(r.job_url));
  });

  // Scraper Form
  scraperTerms = signal(
    'Software Engineer, Solutions Architect, DevOps Engineer, Cloud Architect, Python Dev',
  );
  scraperLocations = signal('Bucharest');
  scraperLimit = signal(20);
  scraperRemote = signal(false);
  scraperRegion = signal('EMEA');
  scraperCountries = signal('');
  scraperAutoApply = signal(false);
  scraperAutonomous = signal(false);
  scraperSites = signal<string[]>([
    'linkedin',
    'indeed',
    'ejobs',
    'bestjobs',
    'hipo',
    'undelucram',
    'jooble',
    'google',
    'greenhouse',
    'lever',
    'remote',
    'glassdoor',
  ]);

  // Console Filters
  consoleCategory = signal('');
  consoleLevel = signal('');
  consoleAutoScroll = signal(true);

  // Activity spinners
  isLoading = signal(false);
  isFunnelLoading = signal(false);
  funnelError = signal<string | null>(null);
  isBatchRunning = signal(false);
  isApplying = signal<Record<string, boolean>>({});
  isPruning = signal(false);
  isPruningEmea = signal(false);
  isPruningDupes = signal(false);
  isSyncingAuth = signal(false);
  isRegeneratingCv = signal<Record<string, boolean>>({});
  launchingPlatform = signal<string | null>(null);

  // Computed Helpers
  hasDegradedResources = computed(() => {
    return Object.values(this.resourceStates()).some((state) => state.error !== null);
  });

  degradedResourceNames = computed(() => {
    const states = this.resourceStates();
    const names: string[] = [];
    const labels: Record<ResourceKey, string> = {
      stats: 'Statistics',
      applications: 'Queue',
      tracker: 'Tracker',
      profile: 'Profile',
      auth: 'Auth Status',
      pipeline: 'Pipeline',
      automation: 'Automation HUD',
    };
    for (const [key, state] of Object.entries(states) as [ResourceKey, ResourceState][]) {
      if (state.error) {
        names.push(labels[key] || key);
      }
    }
    return names;
  });

  primaryDegradedMessage = computed(() => {
    const states = this.resourceStates();
    const errors = Object.values(states)
      .map((s) => s.error)
      .filter((e): e is ClassifiedError => Boolean(e));

    if (errors.length === 0) return '';
    // Priority order: routing (404), network (0), server (5xx), client
    const routingErr = errors.find((e) => e.category === 'routing');
    if (routingErr) return routingErr.message;
    const networkErr = errors.find((e) => e.category === 'network');
    if (networkErr) return networkErr.message;
    const serverErr = errors.find((e) => e.category === 'server');
    if (serverErr) return serverErr.message;
    return errors[0].message;
  });

  lastAnySuccess = computed(() => {
    const states = this.resourceStates();
    let latest: Date | null = null;
    for (const s of Object.values(states)) {
      if (s.lastSuccess && (!latest || s.lastSuccess > latest)) {
        latest = s.lastSuccess;
      }
    }
    return latest ? latest.toLocaleTimeString() : null;
  });

  // Computed Logs
  filteredLogs = computed(() => {
    const logs = this.pipelineStatus()?.logs || [];
    const cat = this.consoleCategory().toLowerCase();
    const lev = this.consoleLevel().toUpperCase();

    return logs.filter((l) => {
      const matchCat = !cat || (l.category || '').toLowerCase() === cat;
      const matchLev = !lev || (l.level || 'INFO').toUpperCase() === lev;
      return matchCat && matchLev;
    });
  });

  // Filtered & Sorted Tracker Records
  filteredTrackerRecords = computed(() => {
    let records = [...this.trackerRecords()];
    const statusFilter = this.trackerFilter().toLowerCase();
    if (statusFilter) {
      records = records.filter((r) => (r.status || '').toLowerCase() === statusFilter);
    }
    const col = this.sortColumn();
    const asc = this.sortAsc();
    records.sort((a: any, b: any) => {
      const valA = (a[col] || '').toString().toLowerCase();
      const valB = (b[col] || '').toString().toLowerCase();
      return asc ? valA.localeCompare(valB) : valB.localeCompare(valA);
    });
    return records;
  });

  ngOnInit() {
    this.notifService.connectEventStream((msg, type) => this.showToast(msg, type));
    this.loadInitialData();
    this.refreshTakeoverStatus();
    // Periodic background poll
    this.pollIntervalId = setInterval(() => {
      this.refreshPoll();
    }, 2000);
  }

  ngOnDestroy() {
    this.notifService.disconnectEventStream();
    if (this.pollIntervalId) {
      clearInterval(this.pollIntervalId);
      this.pollIntervalId = null;
    }
    this.stopCountdownTimer();
    if (this.takeoverTimerId) {
      clearInterval(this.takeoverTimerId);
      this.takeoverTimerId = null;
    }
    this.stopResumePolling();
  }

  setResourceStatus(
    key: ResourceKey,
    status: ResourceStateStatus,
    error: ClassifiedError | null = null,
    updateSuccessTime = false,
  ) {
    this.resourceStates.update((states) => ({
      ...states,
      [key]: {
        status,
        error: status === 'loading' ? states[key].error : error,
        lastSuccess: updateSuccessTime ? new Date() : states[key].lastSuccess,
      },
    }));

    if (status === 'error' && !this.isRetrying()) {
      this.startCountdownTimer();
    } else if (status === 'ready') {
      this.checkClearDegraded();
    }
  }

  private checkClearDegraded() {
    const states = this.resourceStates();
    const hasAnyError = Object.values(states).some((state) => state.error !== null);
    if (!hasAnyError) {
      this.stopCountdownTimer();
      this.retryAttempt.set(1);
    }
  }

  private startCountdownTimer() {
    if (this.countdownTimerId) return;

    const stepIndex = Math.min(this.retryAttempt() - 1, RETRY_BACKOFF_STEPS.length - 1);
    const delaySec = RETRY_BACKOFF_STEPS[Math.max(0, stepIndex)];
    this.retryCountdown.set(delaySec);

    this.countdownTimerId = setInterval(() => {
      const current = this.retryCountdown();
      if (current <= 1) {
        this.stopCountdownTimer();
        this.retryFailedResources();
      } else {
        this.retryCountdown.set(current - 1);
      }
    }, 1000);
  }

  private stopCountdownTimer() {
    if (this.countdownTimerId) {
      clearInterval(this.countdownTimerId);
      this.countdownTimerId = null;
    }
  }

  /**
   * Loads all initial dashboard data in parallel.
   * Keeps `isLoading` true until all initial calls settle.
   * Preserves previously loaded data on failures and tracks per-resource error state.
   */
  async loadInitialData(): Promise<void> {
    this.isLoading.set(true);

    const observables = [
      this.fetchStats(),
      this.fetchApplications(),
      ...(typeof this.api.getAutomationFunnel === 'function' ? [this.fetchAutomationFunnel()] : []),
      this.fetchTracker(),
      this.fetchProfile(),
      this.fetchAuthStatus(),
    ];

    return new Promise((resolve) => {
      forkJoin(observables).subscribe({
        next: () => {
          this.isLoading.set(false);
          resolve();
        },
        error: () => {
          this.isLoading.set(false);
          resolve();
        },
      });
    });
  }

  /**
   * Manual or automatic retry of all degraded resources.
   * Resets retry countdown and triggers fetches for failed items.
   */
  async retryFailedResources(): Promise<void> {
    if (this.isRetrying()) return;
    this.isRetrying.set(true);
    this.stopCountdownTimer();

    const states = this.resourceStates();
    const tasks: Observable<unknown>[] = [];

    if (states.stats.error) tasks.push(this.fetchStats());
    if (states.applications.error) tasks.push(this.fetchApplications());
    if (states.tracker.error) tasks.push(this.fetchTracker());
    if (states.profile.error) tasks.push(this.fetchProfile());
    if (states.auth.error) tasks.push(this.fetchAuthStatus());
    if (states.pipeline.error) tasks.push(this.fetchPipelineStatus());
    if (states.automation.error) tasks.push(this.fetchAutomationStatus());
    if (this.funnelError()) tasks.push(this.fetchAutomationFunnel());

    if (tasks.length === 0) {
      // If none specifically marked error, refresh all core
      tasks.push(
        this.fetchStats(),
        this.fetchApplications(),
        this.fetchTracker(),
        this.fetchProfile(),
        this.fetchAuthStatus(),
        ...(typeof this.api.getAutomationFunnel === 'function'
          ? [this.fetchAutomationFunnel()]
          : []),
      );
    }

    return new Promise((resolve) => {
      forkJoin(tasks).subscribe({
        next: () => {
          this.isRetrying.set(false);
          const stillFailing = Object.values(this.resourceStates()).some(
            (state) => state.error !== null,
          );
          if (stillFailing) {
            this.retryAttempt.update((a) => Math.min(a + 1, RETRY_BACKOFF_STEPS.length));
            this.stopCountdownTimer();
            this.startCountdownTimer();
          } else {
            this.retryAttempt.set(1);
          }
          resolve();
        },
        error: () => {
          this.isRetrying.set(false);
          this.retryAttempt.update((a) => Math.min(a + 1, RETRY_BACKOFF_STEPS.length));
          this.stopCountdownTimer();
          this.startCountdownTimer();
          resolve();
        },
      });
    });
  }

  fetchStats() {
    this.setResourceStatus('stats', 'loading');
    return this.api.getStats().pipe(
      tap((data) => {
        this.stats.set(data);
        this.setResourceStatus('stats', 'ready', null, true);
      }),
      catchError((err) => {
        this.setResourceStatus('stats', 'error', classifyHttpError(err, '/api/stats'));
        return of(null);
      }),
    );
  }

  loadStats() {
    this.fetchStats().subscribe();
  }

  fetchAutomationFunnel() {
    this.isFunnelLoading.set(true);
    return this.api.getAutomationFunnel().pipe(
      tap((data) => {
        this.automationFunnel.set(data);
        this.funnelError.set(null);
        this.isFunnelLoading.set(false);
      }),
      catchError(() => {
        this.funnelError.set('Automation metrics are temporarily unavailable.');
        this.isFunnelLoading.set(false);
        return of(null);
      }),
    );
  }

  loadAutomationFunnel() {
    if (typeof this.api.getAutomationFunnel === 'function') {
      this.fetchAutomationFunnel().subscribe();
    }
  }

  fetchApplications() {
    this.setResourceStatus('applications', 'loading');
    return this.api.getApplications(this.searchQuery()).pipe(
      tap((data) => {
        this.applications.set(data.items || []);
        this.setResourceStatus('applications', 'ready', null, true);
      }),
      catchError((err) => {
        this.setResourceStatus(
          'applications',
          'error',
          classifyHttpError(err, '/api/applications'),
        );
        return of(null);
      }),
    );
  }

  loadApplications() {
    this.fetchApplications().subscribe();
  }

  automationStateLabel(app: ApplicationItem): string {
    const state = app.automation_state || app.status;
    if (!state) return 'Not queued';
    return state.replace(/_/g, ' ');
  }

  automationStepLabel(app: ApplicationItem): string {
    const history = this.automationHistory()[app.id];
    const latest = history?.jobs?.[0];
    const step = latest?.step || latest?.state || app.automation_state;
    return step ? step.replace(/_/g, ' ') : 'Awaiting attempt';
  }

  isAutomationHistoryOpen(appId: string): boolean {
    return Boolean(this.automationHistoryOpen()[appId]);
  }

  toggleAutomationHistory(app: ApplicationItem) {
    const open = this.isAutomationHistoryOpen(app.id);
    this.automationHistoryOpen.update((state) => ({ ...state, [app.id]: !open }));
    if (!open && !this.automationHistory()[app.id]) {
      this.loadAutomationHistory(app);
    }
  }

  loadMoreAutomationEvents(app: ApplicationItem) {
    const current = this.automationHistory()[app.id];
    if (!current || current.loading || !current.hasMore) return;
    this.api.getApplicationAutomationEvents(app.id, app.job_id, current.nextAfter, 50).subscribe({
      next: (response) => {
        this.automationHistory.update((state) => ({
          ...state,
          [app.id]: {
            ...current,
            events: [...current.events, ...(response.events || [])],
            nextAfter: response.next_after,
            hasMore: response.has_more,
            loading: false,
          },
        }));
      },
      error: () => {
        this.automationHistory.update((state) => ({
          ...state,
          [app.id]: { ...current, loading: false, error: 'Unable to load more history.' },
        }));
      },
    });
  }

  private loadAutomationHistory(app: ApplicationItem) {
    const empty = {
      jobs: [] as AutomationJobStatus[],
      events: [] as AutomationEvent[],
      nextAfter: 0,
      hasMore: false,
      loading: true,
      error: null as string | null,
    };
    this.automationHistory.update((state) => ({ ...state, [app.id]: empty }));
    forkJoin({
      status: this.api.getApplicationAutomationStatus(app.id, app.job_id),
      events: this.api.getApplicationAutomationEvents(app.id, app.job_id, 0, 50),
    }).subscribe({
      next: (response) => {
        this.automationHistory.update((state) => ({
          ...state,
          [app.id]: {
            jobs: response.status.jobs || [],
            events: response.events.events || [],
            nextAfter: response.events.next_after,
            hasMore: response.events.has_more,
            loading: false,
            error: null,
          },
        }));
      },
      error: () => {
        this.automationHistory.update((state) => ({
          ...state,
          [app.id]: { ...empty, loading: false, error: 'Automation history is unavailable.' },
        }));
      },
    });
  }

  fetchTracker() {
    this.setResourceStatus('tracker', 'loading');
    return this.api.getTracker(this.trackerFilter()).pipe(
      tap((data) => {
        this.trackerRecords.set(data.records || []);
        this.setResourceStatus('tracker', 'ready', null, true);
      }),
      catchError((err) => {
        this.setResourceStatus('tracker', 'error', classifyHttpError(err, '/api/tracker'));
        return of(null);
      }),
    );
  }

  loadTracker() {
    this.fetchTracker().subscribe();
  }

  fetchProfile() {
    this.setResourceStatus('profile', 'loading');
    return this.api.getProfile().pipe(
      tap((data) => {
        this.profile.set(data);
        this.setResourceStatus('profile', 'ready', null, true);
      }),
      catchError((err) => {
        this.setResourceStatus('profile', 'error', classifyHttpError(err, '/api/profile'));
        return of(null);
      }),
    );
  }

  loadProfile() {
    this.fetchProfile().subscribe();
  }

  fetchAuthStatus() {
    this.setResourceStatus('auth', 'loading');
    return this.api.getAuthStatus().pipe(
      tap((data) => {
        this.authStatus.set(data);
        this.setResourceStatus('auth', 'ready', null, true);
      }),
      catchError((err) => {
        this.setResourceStatus('auth', 'error', classifyHttpError(err, '/api/auth/status'));
        return of(null);
      }),
    );
  }

  checkAuthStatus() {
    this.fetchAuthStatus().subscribe();
  }

  fetchPipelineStatus() {
    this.setResourceStatus('pipeline', 'loading');
    return this.api.getPipelineStatus().pipe(
      tap((data) => {
        this.pipelineStatus.set(data);
        this.setResourceStatus('pipeline', 'ready', null, true);
        if (this.consoleAutoScroll() && this.activeTab() === 'console' && this.consoleContainer) {
          setTimeout(() => {
            const el = this.consoleContainer?.nativeElement;
            if (el) el.scrollTop = el.scrollHeight;
          }, 50);
        }
      }),
      catchError((err) => {
        this.setResourceStatus('pipeline', 'error', classifyHttpError(err, '/api/pipeline/status'));
        return of(null);
      }),
    );
  }

  isAuthenticationWaiting(): boolean {
    return this.automationStatus()?.step === 'auth_required';
  }

  automationBannerTitle(): string {
    if (this.notifService.isStopped()) return 'Automation Stopped (Emergency Stop)';
    if (this.isAuthenticationWaiting()) return 'Waiting for Authentication';
    return 'Automation Paused';
  }

  automationBannerMessage(): string {
    if (this.notifService.isStopped()) {
      return 'Emergency stop engaged. Worker operations halted.';
    }
    if (this.isAuthenticationWaiting()) {
      if (this.automationStatus()?.browser_is_closed || !this.automationStatus()?.browser_active) {
        return 'Authentication is required, but the previous browser closed. Open Browser View and choose Reopen Authentication.';
      }
      return 'Authentication required. Open Browser View and log in; automation remains paused.';
    }
    return 'Automation is paused awaiting operator intervention or safe resume.';
  }

  fetchAutomationStatus() {
    this.setResourceStatus('automation', 'loading');
    return this.api.getAutomationStatus().pipe(
      tap((data) => {
        this.automationStatus.set(data);
        if (data) {
          if (typeof data.is_paused === 'boolean') {
            this.notifService.isPaused.set(data.is_paused);
          }
          if (typeof data.is_stopped === 'boolean') {
            this.notifService.isStopped.set(data.is_stopped);
          }
          if (data.active_job) {
            this.notifService.activeJob.set(data.active_job);
          }
        }
        this.setResourceStatus('automation', 'ready', null, true);
      }),
      catchError((err) => {
        this.setResourceStatus(
          'automation',
          'error',
          classifyHttpError(err, '/api/automation/status'),
        );
        return of(null);
      }),
    );
  }

  /**
   * Background polling: silently updates healthy resources while failed reads use backoff.
   * Suppresses noisy error toasts to avoid toast storms when polling fails.
   */
  refreshPoll() {
    const states = this.resourceStates();
    if (!states.stats.error) this.loadStats();
    if (!states.pipeline.error) this.fetchPipelineStatus().subscribe();
    if (!states.automation.error) this.fetchAutomationStatus().subscribe();
  }

  resolveFileUrl(url: string | null | undefined): string {
    return resolveFileUrl(url);
  }

  resolveSafeFileUrl(url: string | null | undefined): SafeResourceUrl {
    return this.sanitizer.bypassSecurityTrustResourceUrl(this.resolveFileUrl(url));
  }

  showToast(message: string, type: 'success' | 'error' | 'info' | 'warning' = 'success') {
    this.toast.set({ visible: true, message, type });
    setTimeout(() => {
      this.toast.set({ visible: false, message: '', type: 'success' });
    }, 4500);
  }

  async copyText(text: string | undefined, msg = 'Copied to clipboard!') {
    if (!text || !text.trim()) {
      this.showToast('No text available to copy', 'error');
      return;
    }
    let success = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        success = true;
      }
    } catch {
      success = false;
    }

    if (!success) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.append(ta);
        ta.focus();
        ta.select();
        success = document.execCommand('copy');
        ta.remove();
      } catch {
        success = false;
      }
    }

    if (success) {
      this.showToast(msg, 'success');
    } else {
      this.showToast('Failed to copy to clipboard', 'error');
    }
  }

  openReviewModal(appId: string) {
    this.api.getApplication(appId).subscribe({
      next: (data) => this.selectedApp.set(data),
      error: () => this.showToast('Failed to load application details', 'error'),
    });
  }

  closeReviewModal() {
    this.selectedApp.set(null);
  }

  saveCoverLetter() {
    const app = this.selectedApp();
    if (!app) return;
    this.api.updateCoverLetter(app.id, app.cover_letter).subscribe({
      next: () => this.showToast('Cover letter updated successfully!'),
      error: () => this.showToast('Failed to save cover letter', 'error'),
    });
  }

  retriggerCvGeneration(appId: string) {
    this.isRegeneratingCv.update((m) => ({ ...m, [appId]: true }));
    this.showToast('Generating tailored CV with Gemini 3.8-Flash...', 'info');
    this.api.regenerateCv(appId).subscribe({
      next: (res) => {
        this.showToast(res.message || 'CV generated successfully!');
        const current = this.selectedApp();
        if (current && current.id === appId) {
          this.selectedApp.set({
            ...current,
            cv_filename: res.cv_filename,
            cv_pdf_url: res.cv_pdf_url,
            cover_letter: res.cover_letter || current.cover_letter,
          });
        }
        this.loadApplications();
        this.isRegeneratingCv.update((m) => ({ ...m, [appId]: false }));
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to generate CV', 'error');
        this.isRegeneratingCv.update((m) => ({ ...m, [appId]: false }));
      },
    });
  }

  applyForJob(appId: string, mode: 'assisted' | 'autonomous') {
    this.isApplying.update((m) => ({ ...m, [appId]: true }));
    this.api.applyForJob(appId, mode).subscribe({
      next: () => {
        this.closeReviewModal();
        this.showToast(`Auto-apply (${mode}) started in browser!`);
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Could not start auto-apply', 'error');
        this.isApplying.update((m) => ({ ...m, [appId]: false }));
      },
    });
  }

  triggerBatchApply() {
    this.isBatchRunning.set(true);
    this.showToast('Batch auto-apply started for next 5 jobs...', 'info');
    this.api.batchApply(5, 'assisted').subscribe({
      next: () => {
        this.showToast('Batch apply launched in background!');
        this.isBatchRunning.set(false);
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to start batch', 'error');
        this.isBatchRunning.set(false);
      },
    });
  }

  markDone(appId: string) {
    this.api.markDone(appId).subscribe({
      next: () => {
        this.showToast('Application marked as done and moved to tracker.');
        this.loadApplications();
        this.loadStats();
      },
    });
  }

  deleteApp(appId: string) {
    if (!confirm('Are you sure you want to dismiss this application?')) return;
    this.api.deleteApplication(appId).subscribe({
      next: () => {
        this.showToast('Application dismissed.');
        this.loadApplications();
        this.loadStats();
      },
    });
  }

  clearFailed() {
    if (!confirm('Clear all failed applications from queue and tracker?')) return;
    this.api.clearFailed().subscribe({
      next: (res) => {
        this.showToast(res.message || 'Cleared failed applications!');
        this.loadApplications();
        this.loadTracker();
        this.loadStats();
      },
    });
  }

  pruneInactive() {
    this.isPruning.set(true);
    this.showToast('Scanning queue to prune expired/closed jobs...', 'info');
    this.api.pruneInactive().subscribe({
      next: (res) => {
        this.showToast(res.message || `Pruned ${res.pruned_count} inactive jobs!`);
        this.loadApplications();
        this.loadStats();
        this.isPruning.set(false);
      },
      error: () => this.isPruning.set(false),
    });
  }

  pruneNonEmea() {
    this.isPruningEmea.set(true);
    this.showToast('Filtering queue strictly for EMEA/Europe positions...', 'info');
    this.api.pruneNonEmea(this.scraperRegion()).subscribe({
      next: (res) => {
        this.showToast(res.message || `Pruned ${res.pruned_count} non-EMEA jobs!`);
        this.loadApplications();
        this.loadStats();
        this.isPruningEmea.set(false);
      },
      error: () => this.isPruningEmea.set(false),
    });
  }

  pruneDuplicates() {
    this.isPruningDupes.set(true);
    this.showToast('Scanning queue for duplicate roles and URLs...', 'info');
    this.api.pruneDuplicates().subscribe({
      next: (res) => {
        this.showToast(res.message || `Pruned ${res.pruned_count} duplicates!`);
        this.loadApplications();
        this.loadStats();
        this.isPruningDupes.set(false);
      },
      error: () => this.isPruningDupes.set(false),
    });
  }

  updateTrackerStatus(jobUrl: string, status: string) {
    this.api.updateTrackerStatus(jobUrl, status).subscribe({
      next: () => {
        this.showToast(`Updated status to ${status}`);
        this.loadTracker();
        this.loadStats();
      },
    });
  }

  isTrackerSelected(url: string): boolean {
    return this.selectedTrackerUrls().has(url);
  }

  toggleTrackerSelection(url: string, event: Event) {
    const checked = (event.target as HTMLInputElement).checked;
    this.selectedTrackerUrls.update((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(url);
      } else {
        next.delete(url);
      }
      return next;
    });
  }

  toggleSelectAllTracker() {
    const records = this.filteredTrackerRecords();
    if (this.isAllTrackerSelected()) {
      this.selectedTrackerUrls.update((current) => {
        const next = new Set(current);
        for (const r of records) {
          if (r.job_url) {
            next.delete(r.job_url);
          }
        }
        return next;
      });
    } else {
      this.selectedTrackerUrls.update((current) => {
        const next = new Set(current);
        for (const r of records) {
          if (r.job_url) {
            next.add(r.job_url);
          }
        }
        return next;
      });
    }
  }

  sendSelectedToQueue() {
    const selectedUrls = Array.from(this.selectedTrackerUrls());
    if (selectedUrls.length === 0) return;

    const records = this.trackerRecords().filter(
      (r) => r.job_url && selectedUrls.includes(r.job_url),
    );
    const items = records.map((r) => ({
      job_url: r.job_url,
      folder_name: r.folder_name || '',
    }));

    this.api.batchRequeue({ items }).subscribe({
      next: (res: any) => {
        const count = res.count ?? items.length;
        this.showToast(`Enqueued ${count} applications to the queue!`, 'success');
        this.selectedTrackerUrls.set(new Set());
        this.loadApplications();
        this.loadTracker();
        this.loadStats();
      },
      error: (err) => {
        this.showToast(
          `Failed to enqueue applications: ${err?.error?.detail || err.message || err}`,
          'error',
        );
      },
    });
  }

  sendAllToQueue() {
    const records = this.filteredTrackerRecords();
    if (records.length === 0) return;

    const count = records.length;
    const filterDesc = this.trackerFilter()
      ? `with status "${this.trackerFilter()}"`
      : 'currently displayed';
    if (
      !confirm(
        `Are you sure you want to send all ${count} applications (${filterDesc}) to the automation queue?`,
      )
    ) {
      return;
    }

    const items = records.map((r) => ({
      job_url: r.job_url,
      folder_name: r.folder_name || '',
    }));

    this.api
      .batchRequeue({
        items,
        requeue_all: false,
        status_filter: this.trackerFilter() || undefined,
      })
      .subscribe({
        next: (res: any) => {
          const countRes = res.count ?? count;
          this.showToast(`Successfully enqueued ${countRes} applications!`, 'success');
          this.selectedTrackerUrls.set(new Set());
          this.loadApplications();
          this.loadTracker();
          this.loadStats();
        },
        error: (err) => {
          this.showToast(
            `Failed to enqueue applications: ${err?.error?.detail || err.message || err}`,
            'error',
          );
        },
      });
  }

  requeueApp(record: TrackerRecord) {
    this.api.requeue(record.job_url, record.folder_name).subscribe({
      next: () => {
        this.showToast('Moved application back to queue!');
        if (record.job_url) {
          this.selectedTrackerUrls.update((s) => {
            const next = new Set(s);
            next.delete(record.job_url);
            return next;
          });
        }
        this.loadApplications();
        this.loadTracker();
        this.loadStats();
      },
    });
  }

  openDiagnostics(appId: string) {
    this.api.getDiagnostics(appId).subscribe({
      next: (data) => {
        this.diagData.set(data);
        this.diagModalOpen.set(true);
      },
      error: () => this.showToast('Diagnostics not available', 'info'),
    });
  }

  toggleSort(col: string) {
    if (this.sortColumn() === col) {
      this.sortAsc.update((a) => !a);
    } else {
      this.sortColumn.set(col);
      this.sortAsc.set(true);
    }
  }

  toggleSite(site: string) {
    const current = this.scraperSites();
    if (current.includes(site)) {
      this.scraperSites.set(current.filter((s) => s !== site));
    } else {
      this.scraperSites.set([...current, site]);
    }
  }

  startPipeline() {
    const terms = this.scraperTerms()
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
    const locations = this.scraperLocations()
      .split(',')
      .map((l) => l.trim())
      .filter(Boolean);
    const countries = this.scraperCountries()
      .split(',')
      .map((c) => c.trim())
      .filter(Boolean);

    this.api
      .runPipeline({
        terms,
        locations,
        limit: this.scraperLimit(),
        sites: this.scraperSites(),
        is_remote: this.scraperRemote(),
        region: this.scraperRegion(),
        countries,
        auto_apply: this.scraperAutoApply(),
        autonomous: this.scraperAutonomous(),
      })
      .subscribe({
        next: () => {
          this.showToast('Pipeline started! Switched to Console tab to monitor output.');
          this.activeTab.set('console');
          this.refreshPoll();
        },
        error: (err) =>
          this.showToast(classifyHttpError(err).message || 'Failed to start pipeline', 'error'),
      });
  }

  clearConsoleLogs() {
    this.api.clearLogs().subscribe({
      next: () => this.showToast('Console logs cleared'),
    });
  }

  submitCode() {
    const code = this.verificationCode().trim();
    if (!code) return;
    this.api.submitVerificationCode(code).subscribe({
      next: () => {
        this.showToast('Verification code submitted to browser!');
        this.verificationCode.set('');
      },
      error: (err) =>
        this.showToast(classifyHttpError(err).message || 'Failed to submit code', 'error'),
    });
  }

  @HostListener('document:keydown.escape')
  onEscapeKey() {
    if (this.moreActionsOpen()) {
      this.closeMoreActions(true);
    }
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent) {
    if (!this.moreActionsOpen()) return;
    const target = event.target as Element | null;
    if (!target?.closest('.more-actions')) {
      this.closeMoreActions(true);
    }
  }

  toggleMoreActions() {
    if (this.moreActionsOpen()) {
      this.closeMoreActions(true);
    } else {
      this.moreActionsOpen.set(true);
    }
  }

  closeMoreActions(restoreFocus = false) {
    this.moreActionsOpen.set(false);
    if (restoreFocus) {
      queueMicrotask(() => this.moreActionsButton?.nativeElement.focus());
    }
  }

  openResumeViewer() {
    this.closeMoreActions();
    this.resumeViewerMode.set('structured');
    this.resumeModalOpen.set(true);
    this.loadMainResume();
  }

  openPdfPreview() {
    this.resumeViewerMode.set('preview');
    if (!this.resumeModalOpen()) {
      this.closeMoreActions();
      this.resumeModalOpen.set(true);
      this.loadMainResume();
    }
  }

  setResumeViewerMode(mode: 'structured' | 'preview') {
    this.resumeViewerMode.set(mode);
  }

  closeResumeViewer() {
    this.resumeModalOpen.set(false);
    this.resumeViewerMode.set('structured');
    this.stopResumePolling();
  }

  retryMainResume() {
    this.loadMainResume();
  }

  private loadMainResume() {
    this.stopResumePolling();
    this.resumeViewStatus.set('loading');
    this.resumeViewError.set(null);
    this.api.getMainResume().subscribe({
      next: (response: MainResumeResponse) => {
        this.applyMainResumeResponse(response);
        if (response.status === 'generating' || response.status === 'stale')
          this.startResumePolling();
      },
      error: (err) => {
        this.resumeViewStatus.set('error');
        this.resumeViewError.set(classifyHttpError(err).message || 'Unable to load main resume.');
      },
    });
  }

  private applyMainResumeResponse(response: MainResumeResponse) {
    this.mainResume.set(response.resume);
    this.resumeArtifactUrl.set(
      response.artifact_url ? resolveFileUrl(response.artifact_url) : null,
    );
    this.resumeArtifactDownloadUrl.set(
      response.download_url
        ? resolveFileUrl(response.download_url)
        : response.artifact_url
          ? `${resolveFileUrl(response.artifact_url)}?download=true`
          : null,
    );
    this.resumeViewStatus.set(response.status);
    this.resumeViewError.set(response.error);
  }

  private startResumePolling() {
    if (this.resumePollTimerId) return;
    this.resumePollAttempts = 0;
    this.resumePollTimerId = setInterval(() => {
      this.resumePollAttempts += 1;
      if (this.resumePollAttempts > 15) {
        this.stopResumePolling();
        this.resumeViewStatus.set('error');
        this.resumeViewError.set('Resume generation is taking longer than expected. Retry.');
        return;
      }
      this.api.getMainResume().subscribe({
        next: (response) => {
          this.applyMainResumeResponse(response);
          if (response.status !== 'generating' && response.status !== 'stale')
            this.stopResumePolling();
        },
        error: () => {
          this.stopResumePolling();
          this.resumeViewStatus.set('error');
          this.resumeViewError.set('Unable to check resume generation status. Retry.');
        },
      });
    }, 2000);
  }

  private stopResumePolling() {
    if (this.resumePollTimerId) {
      clearInterval(this.resumePollTimerId);
      this.resumePollTimerId = null;
    }
    this.resumePollAttempts = 0;
  }

  openAuthModal() {
    this.closeMoreActions();
    this.checkAuthStatus();
    this.authModalOpen.set(true);
  }

  launchPlatformLogin(platform: string) {
    this.launchingPlatform.set(platform);
    this.api.launchAuthLogin(platform, 180).subscribe({
      next: () => {
        this.showToast(
          `Chrome opened for ${platform.toUpperCase()} on DISPLAY. Complete login in browser!`,
        );
        setTimeout(() => this.launchingPlatform.set(null), 3000);
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to open login', 'error');
        this.launchingPlatform.set(null);
      },
    });
  }

  syncDesktopChrome() {
    this.isSyncingAuth.set(true);
    this.showToast('Importing sessions from desktop Chrome...', 'info');
    this.api.syncDesktopChrome().subscribe({
      next: (res) => {
        this.showToast(`Successfully imported ${res.cookies_merged} cookies!`);
        this.checkAuthStatus();
        this.isSyncingAuth.set(false);
      },
      error: () => this.isSyncingAuth.set(false),
    });
  }

  uploadBackup(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return Promise.resolve();

    const fd = new FormData();
    fd.append('file', file);
    this.isImporting.set(true);
    this.showToast('Importing backup package...', 'info');

    return new Promise((resolve) => {
      this.api.importBackup(fd).subscribe({
        next: async (res) => {
          const report = res['report'] as any;
          const importedApps =
            report?.imported_applications ?? (res['imported_applications'] as number) ?? 0;
          const mergedRecords =
            report?.merged_db_records ?? (res['merged_db_records'] as number) ?? 0;
          const importedApplied =
            report?.imported_applied ?? (res['imported_applied'] as number) ?? 0;

          // Clear search and filter so newly imported applications aren't hidden
          this.searchQuery.set('');
          this.trackerFilter.set('');

          try {
            await this.loadInitialData();
            this.isImporting.set(false);

            if (this.hasDegradedResources()) {
              this.showToast(
                `Imported ${importedApps} applications, but dashboard refresh failed. Please click Retry now.`,
                'warning',
              );
              resolve();
              return;
            }

            if (importedApps === 0 && mergedRecords === 0 && importedApplied === 0) {
              this.showToast(
                'Backup package imported, but 0 applications or records were found in the archive.',
                'warning',
              );
            } else if (importedApps > 0) {
              this.activeTab.set('queue');
              this.showToast(
                `Successfully imported ${importedApps} applications and ${mergedRecords} records!`,
                'success',
              );
            } else {
              this.activeTab.set('tracker');
              this.showToast(`Successfully imported ${mergedRecords} tracker records!`, 'success');
            }
          } catch {
            this.isImporting.set(false);
            this.showToast(
              `Imported ${importedApps} applications, but dashboard refresh failed. Please click Retry now.`,
              'warning',
            );
          }
          resolve();
        },
        error: (err) => {
          this.isImporting.set(false);
          const classified = classifyHttpError(err, '/api/import');
          this.showToast(classified.message || 'Import failed', 'error');
          resolve();
        },
      });

      input.value = '';
    });
  }

  saveProfile() {
    const p = this.profile();
    if (!p) return;
    this.api.updateProfile(p).subscribe({
      next: () => this.showToast('Candidate profile saved successfully!'),
      error: () => this.showToast('Failed to save profile', 'error'),
    });
  }

  // --- Notifications Drawer & Opt-In Controls ---

  openNotificationsDrawer() {
    this.notificationsDrawerOpen.set(true);
    this.notifService.fetchNotifications();
  }

  closeNotificationsDrawer() {
    this.notificationsDrawerOpen.set(false);
  }

  openNotifSettingsModal() {
    this.notifSettingsModalOpen.set(true);
  }

  closeNotifSettingsModal() {
    this.notifSettingsModalOpen.set(false);
  }

  ackNotification(id: number | string) {
    this.notifService.ackNotification(id);
    this.showToast('Notification acknowledged', 'info');
  }

  ackAllNotifications() {
    this.notifService.ackAll();
    this.showToast('All notifications acknowledged', 'info');
  }

  dismissNotification(id: number | string) {
    this.notifService.dismissNotification(id);
    this.showToast('Notification dismissed', 'info');
  }

  clearAllNotifications() {
    this.notifService.clearAll();
    this.showToast('All notifications cleared', 'info');
  }

  // --- Automation Runtime Controls (Pause / Resume / Stop) ---

  pauseAutomation() {
    this.isPausingAutomation.set(true);
    this.api.pauseAutomation().subscribe({
      next: () => {
        this.notifService.isPaused.set(true);
        this.showToast('Automation paused. Active browser state preserved.', 'warning');
        this.isPausingAutomation.set(false);
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to pause automation', 'error');
        this.isPausingAutomation.set(false);
      },
    });
  }

  resumeAutomation() {
    this.isResumingAutomation.set(true);
    this.api.resumeAutomation().subscribe({
      next: (res) => {
        this.notifService.isPaused.set(false);
        this.showToast(res.message || 'Automation resumed after safe revalidation!', 'success');
        this.isResumingAutomation.set(false);
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(
          classifyHttpError(err).message || 'Resume rejected: revalidation checks failed',
          'error',
        );
        this.isResumingAutomation.set(false);
      },
    });
  }

  stopAutomation() {
    if (!confirm('Engage Emergency Stop? All active worker operations will immediately halt.'))
      return;
    this.isStoppingAutomation.set(true);
    this.api.stopAutomation().subscribe({
      next: () => {
        this.notifService.isStopped.set(true);
        this.notifService.isPaused.set(true);
        this.showToast('EMERGENCY STOP engaged. All operations halted.', 'error');
        this.isStoppingAutomation.set(false);
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to stop automation', 'error');
        this.isStoppingAutomation.set(false);
      },
    });
  }

  clearAutomationState() {
    if (
      !confirm(
        'Clear automation state? This resets any stuck or claimed jobs back to ready, closes orphaned browser sessions, and clears emergency stop.',
      )
    )
      return;
    this.isClearingAutomationState.set(true);
    this.api.clearAutomationState().subscribe({
      next: (res) => {
        this.notifService.isStopped.set(false);
        this.notifService.isPaused.set(false);
        this.showToast(res.message || 'Automation state cleared successfully!', 'success');
        this.isClearingAutomationState.set(false);
        this.loadApplications();
        this.loadTracker();
        this.loadStats();
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(
          classifyHttpError(err).message || 'Failed to clear automation state',
          'error',
        );
        this.isClearingAutomationState.set(false);
      },
    });
  }

  // --- Queue Job Actions (Skip / Cancel / Resolve) ---

  skipActiveJob(jobId: string) {
    if (!confirm(`Skip job ${jobId}?`)) return;
    this.api.skipJob(jobId).subscribe({
      next: () => {
        this.showToast(`Job ${jobId} marked as skipped.`);
        this.refreshPoll();
        this.loadApplications();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to skip job', 'error');
      },
    });
  }

  cancelActiveJob(jobId: string) {
    if (!confirm(`Cancel job ${jobId}?`)) return;
    this.api.cancelJob(jobId).subscribe({
      next: () => {
        this.showToast(`Job ${jobId} cancelled.`);
        this.refreshPoll();
        this.loadApplications();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to cancel job', 'error');
      },
    });
  }

  openResolveModal(jobId: string, questionKey = '') {
    this.resolveJobId.set(jobId);
    this.resolveQuestionKey.set(questionKey);
    this.resolveAnswerValue.set('');
    this.resolveApprovedScope.set('global');
    this.resolveModalOpen.set(true);
  }

  closeResolveModal() {
    this.resolveModalOpen.set(false);
  }

  submitResolution() {
    const jid = this.resolveJobId();
    if (!jid) return;
    this.isResolvingJob.set(true);
    this.api
      .resolveJob(
        jid,
        'answer',
        this.resolveAnswerValue(),
        this.resolveQuestionKey(),
        this.resolveApprovedScope(),
      )
      .subscribe({
        next: () => {
          this.showToast(`Job ${jid} resolved with approved answer and requeued!`, 'success');
          this.closeResolveModal();
          this.isResolvingJob.set(false);
          this.refreshPoll();
        },
        error: (err) => {
          this.showToast(classifyHttpError(err).message || 'Failed to resolve job', 'error');
          this.isResolvingJob.set(false);
        },
      });
  }

  // --- noVNC Viewer & Operator Takeover Controls ---

  refreshTakeoverStatus() {
    this.api
      .getTakeoverStatus()
      .pipe(catchError(() => of(null)))
      .subscribe({
        next: (status) => {
          if (status) {
            this.takeoverStatus.set(status);
            if (status.is_takeover_active) {
              this.startTakeoverTimer();
            } else {
              this.stopTakeoverTimer();
            }
          }
        },
      });
  }

  private startTakeoverTimer() {
    if (this.takeoverTimerId) return;
    this.takeoverTimerId = setInterval(() => {
      const current = this.takeoverCountdown();
      if (current <= 1) {
        this.stopTakeoverTimer();
        this.refreshTakeoverStatus();
      } else {
        this.takeoverCountdown.set(current - 1);
      }
    }, 1000);
  }

  private stopTakeoverTimer() {
    if (this.takeoverTimerId) {
      clearInterval(this.takeoverTimerId);
      this.takeoverTimerId = null;
    }
  }

  openTakeoverModal() {
    this.vncModalOpen.set(true);
    this.refreshTakeoverStatus();
  }

  closeTakeoverModal() {
    this.vncModalOpen.set(false);
  }

  claimTakeover() {
    this.isClaimingTakeover.set(true);
    this.api.claimTakeover('operator', 300).subscribe({
      next: (res) => {
        this.showToast('Exclusive operator takeover claimed! Automation paused.', 'warning');
        this.isClaimingTakeover.set(false);
        this.takeoverCountdown.set(res.lease_seconds || 300);
        this.refreshTakeoverStatus();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to claim takeover lease', 'error');
        this.isClaimingTakeover.set(false);
      },
    });
  }

  releaseTakeover() {
    this.isReleasingTakeover.set(true);
    this.api.releaseTakeover('operator', false).subscribe({
      next: () => {
        this.showToast(
          'Takeover lease released. Automation remains paused awaiting safe resume revalidation.',
          'info',
        );
        this.isReleasingTakeover.set(false);
        this.stopTakeoverTimer();
        this.refreshTakeoverStatus();
      },
      error: (err) => {
        this.showToast(classifyHttpError(err).message || 'Failed to release takeover', 'error');
        this.isReleasingTakeover.set(false);
      },
    });
  }

  reopenAuthSession() {
    this.isReopeningAuth.set(true);
    const jobId = (this.automationStatus()?.active_job as { id?: string } | undefined)?.id;
    this.api.reopenAuthSession(jobId).subscribe({
      next: (res) => {
        this.showToast(
          res.message || 'Fresh browser session requested. Waiting for authentication challenge.',
          'info',
        );
        this.isReopeningAuth.set(false);
        this.closeTakeoverModal();
        this.refreshTakeoverStatus();
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(
          classifyHttpError(err).message || 'Unable to reopen authentication session',
          'error',
        );
        this.isReopeningAuth.set(false);
      },
    });
  }

  resumeFromTakeover() {
    this.isResumingTakeover.set(true);
    this.api.resumeTakeover().subscribe({
      next: (res) => {
        this.showToast(
          res.message || 'Safe resume revalidation succeeded! Automation unpaused.',
          'success',
        );
        this.isResumingTakeover.set(false);
        this.closeTakeoverModal();
        this.refreshTakeoverStatus();
        this.refreshPoll();
      },
      error: (err) => {
        this.showToast(
          classifyHttpError(err).message || 'Safe resume rejected: revalidation checks failed',
          'error',
        );
        this.isResumingTakeover.set(false);
      },
    });
  }

  getPlatformBadgeClass(platform: string): string {
    const p = (platform || '').toLowerCase();
    if (p.includes('greenhouse'))
      return 'bg-emerald-950 text-emerald-300 border border-emerald-800';
    if (p.includes('lever')) return 'bg-purple-950 text-purple-300 border border-purple-800';
    if (p.includes('workday')) return 'bg-blue-950 text-blue-300 border border-blue-800';
    if (p.includes('indeed')) return 'bg-indigo-950 text-indigo-300 border border-indigo-800';
    if (p.includes('linkedin')) return 'bg-sky-950 text-sky-300 border border-sky-800';
    return 'bg-slate-800 text-slate-300 border border-slate-700';
  }
}
