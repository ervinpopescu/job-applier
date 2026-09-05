import {
  Component,
  type ElementRef,
  type OnDestroy,
  type OnInit,
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
  type AuthStatusReport,
  type AutomationStatus,
  type CandidateProfile,
  type ClassifiedError,
  type PipelineStatus,
  type ResourceKey,
  type ResourceState,
  type ResourceStateStatus,
  type ToastNotification,
  type TrackerRecord,
  type TrackerStats,
  classifyHttpError,
} from './models/types';

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
  private sanitizer = inject(DomSanitizer);

  @ViewChild('consoleContainer') consoleContainer?: ElementRef<HTMLDivElement>;

  // Tabs
  activeTab = signal<'queue' | 'tracker' | 'scraper' | 'console' | 'profile'>('queue');

  // Core Data Signals
  stats = signal<TrackerStats | null>(null);
  applications = signal<ApplicationItem[]>([]);
  selectedApp = signal<ApplicationDetail | null>(null);
  trackerRecords = signal<TrackerRecord[]>([]);
  pipelineStatus = signal<PipelineStatus | null>(null);
  automationStatus = signal<AutomationStatus | null>(null);
  profile = signal<CandidateProfile | null>(null);
  authStatus = signal<AuthStatusReport | null>(null);

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
    this.loadInitialData();
    // Periodic background poll
    this.pollIntervalId = setInterval(() => {
      this.refreshPoll();
    }, 2000);
  }

  ngOnDestroy() {
    if (this.pollIntervalId) {
      clearInterval(this.pollIntervalId);
      this.pollIntervalId = null;
    }
    this.stopCountdownTimer();
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

    if (tasks.length === 0) {
      // If none specifically marked error, refresh all core
      tasks.push(
        this.fetchStats(),
        this.fetchApplications(),
        this.fetchTracker(),
        this.fetchProfile(),
        this.fetchAuthStatus(),
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

  fetchAutomationStatus() {
    this.setResourceStatus('automation', 'loading');
    return this.api.getAutomationStatus().pipe(
      tap((data) => {
        this.automationStatus.set(data);
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

  requeueApp(record: TrackerRecord) {
    this.api.requeue(record.job_url, record.folder_name).subscribe({
      next: () => {
        this.showToast('Moved application back to queue!');
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

  openAuthModal() {
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
