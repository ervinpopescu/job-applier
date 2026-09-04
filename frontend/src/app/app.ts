import {
  Component,
  type ElementRef,
  type OnInit,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { IconComponent } from './components/icon.component';
import { ApiService } from './services/api.service';
import type {
  ApplicationDetail,
  ApplicationItem,
  AuthStatusReport,
  AutomationStatus,
  CandidateProfile,
  PipelineStatus,
  TrackerRecord,
  TrackerStats,
} from './models/types';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App implements OnInit {
  private api = inject(ApiService);

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

  // Modals & Banners
  authModalOpen = signal(false);
  diagModalOpen = signal(false);
  diagData = signal<Record<string, unknown> | null>(null);
  toast = signal<{ visible: boolean; message: string; type: string }>({
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
    // Periodic refresh
    setInterval(() => {
      this.refreshPoll();
    }, 2000);
  }

  loadInitialData() {
    this.isLoading.set(true);
    this.loadStats();
    this.loadApplications();
    this.loadTracker();
    this.loadProfile();
    this.checkAuthStatus();
    this.isLoading.set(false);
  }

  refreshPoll() {
    this.loadStats();
    this.api.getPipelineStatus().subscribe({
      next: (data) => {
        this.pipelineStatus.set(data);
        if (this.consoleAutoScroll() && this.activeTab() === 'console' && this.consoleContainer) {
          setTimeout(() => {
            const el = this.consoleContainer?.nativeElement;
            if (el) el.scrollTop = el.scrollHeight;
          }, 50);
        }
      },
    });
    this.api.getAutomationStatus().subscribe({
      next: (data) => this.automationStatus.set(data),
    });
  }

  loadStats() {
    this.api.getStats().subscribe({
      next: (data) => this.stats.set(data),
    });
  }

  loadApplications() {
    this.api.getApplications(this.searchQuery()).subscribe({
      next: (data) => this.applications.set(data.items || []),
    });
  }

  loadTracker() {
    this.api.getTracker(this.trackerFilter()).subscribe({
      next: (data) => this.trackerRecords.set(data.records || []),
    });
  }

  loadProfile() {
    this.api.getProfile().subscribe({
      next: (data) => this.profile.set(data),
    });
  }

  checkAuthStatus() {
    this.api.getAuthStatus().subscribe({
      next: (data) => this.authStatus.set(data),
    });
  }

  showToast(message: string, type: 'success' | 'error' | 'info' = 'success') {
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
        this.showToast(err.error?.detail || 'Failed to generate CV', 'error');
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
        this.showToast(err.error?.detail || 'Could not start auto-apply', 'error');
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
        this.showToast(err.error?.detail || 'Failed to start batch', 'error');
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
        error: (err) => this.showToast(err.error?.detail || 'Failed to start pipeline', 'error'),
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
      error: (err) => this.showToast(err.error?.detail || 'Failed to submit code', 'error'),
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
        this.showToast(err.error?.detail || 'Failed to open login', 'error');
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

  uploadBackup(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    const fd = new FormData();
    fd.append('file', file);
    this.showToast('Importing backup package...', 'info');
    this.api.importBackup(fd).subscribe({
      next: (res) => {
        this.showToast((res['message'] as string) || 'Backup imported successfully!');
        this.loadInitialData();
      },
      error: (err) => this.showToast(err.error?.detail || 'Import failed', 'error'),
    });
    input.value = '';
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
