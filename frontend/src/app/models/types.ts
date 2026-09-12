export interface ApplicationItem {
  id: string;
  company: string;
  title: string;
  job_url: string;
  cv_filename: string;
  has_cover_letter: boolean;
  cover_letter_preview: string;
  pdf_url: string;
  created_at: string;
  platform?: string;
  status?: string;
  submission_type?: string;
  automation_state?: string | null;
  job_state?: string | null;
  job_id?: string | null;
  lease_owner?: string | null;
  retry_count?: number | null;
  checkpoint?: string | null;
  attempt_status?: string | null;
  error_message?: string | null;
  folder_name?: string;
}

export interface AppFilterCounts {
  all: number;
  queued: number;
  action_required: number;
  pending: number;
  applied: number;
  skipped: number;
  failed: number;
}

export interface AutomationJobStatus {
  job_id: string;
  app_id: string;
  attempt_number?: number | null;
  state?: string | null;
  step?: string | null;
  outcome_code?: string | null;
  event_cursor?: number | null;
  updated_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  submit_intent_at?: string | null;
}

export interface AutomationEvent {
  id: number;
  app_id: string;
  job_id?: string | null;
  event_type: string;
  message: string;
  created_at: string;
  step?: string | null;
  outcome_code?: string | null;
}

export interface ApplicationAutomationStatusResponse {
  app_id: string;
  jobs: AutomationJobStatus[];
}

export interface ApplicationAutomationEventsResponse {
  app_id: string;
  events: AutomationEvent[];
  next_after: number;
  has_more: boolean;
}

export interface AutomationFunnel {
  verified_autonomous_submissions: number;
  jobs_tracked: number;
  runnable_jobs: number;
  /** @deprecated Use jobs_tracked. */
  jobs_queued: number;
  jobs_with_attempt: number;
  current_auth_blocked_jobs: number;
  current_site_changed_jobs: number;
  historical_auth_blocked_attempts: number;
  historical_site_changed_attempts: number;
  total_attempts: number;
  current_permanent_failures: number;
  current_retry_wait_jobs: number;
  current_skipped_jobs: number;
  generated_artifacts: number;
  manual_applied: number;
  applied_artifacts: number;
  unqueued_artifacts: number;
  // Deprecated compatibility aliases retained for older API consumers.
  jobs_attempted: number;
  auth_required: number;
  site_changed: number;
  permanent_failures: number;
  retries: number;
  unknown: number;
  jobs_total: number;
  attempts_total: number;
  deprecated_fields: string[];
  job_states: Record<string, number>;
  attempt_outcomes: Record<string, number>;
}

export interface MainResume {
  contact: Record<string, string>;
  summary: string;
  experience: Array<{
    role: string;
    company: string;
    dates: string;
    details: string[];
  }>;
  skills: string[];
  education: {
    institution?: string;
    degree?: string;
    details?: string;
  };
  projects: Array<{
    name: string;
    description: string;
    url: string;
  }>;
}

export interface MainResumeResponse {
  status: 'ready' | 'stale' | 'generating' | 'error';
  resume: MainResume;
  artifact_url: string | null;
  download_url?: string | null;
  generation_id: string | null;
  updated_at: string | null;
  error: string | null;
  retry_after_seconds: number | null;
}

export interface ApplicationDetail {
  id: string;
  company: string;
  title: string;
  job_url: string;
  cv_filename: string;
  cv_pdf_url: string;
  cover_letter: string;
  tailored_resume: Record<string, unknown>;
  bookmarklet: string;
  proof_screenshot_url: string;
  fail_screenshot_url: string;
}

export interface TrackerRecord {
  timestamp: string;
  company: string;
  title: string;
  job_url: string;
  platform: string;
  status: string;
  submission_type: string;
  cv_path?: string;
  proof_path?: string;
  notes?: string;
  folder_name?: string;
}

export interface TrackerStats {
  pending: number;
  applied_folders: number;
  tracker: {
    total_records: number;
    applied: number;
    auto_filled: number;
    skipped: number;
    failed: number;
    platforms: Record<string, number>;
  };
  pipeline: {
    is_running: boolean;
    status: string;
  };
  queue?: {
    active_jobs?: number;
    total_jobs?: number;
    ready?: number;
    claimed?: number;
    in_progress?: number;
    retry_wait?: number;
    exceptions?: number;
  };
}

export interface LogEntry {
  time: string;
  level: 'INFO' | 'WARN' | 'ERROR' | 'SUCCESS';
  category: string;
  message: string;
}

export interface PipelineStatus {
  is_running: boolean;
  status: string;
  started_at: string;
  logs: LogEntry[];
  error?: string;
}

export interface AutomationStatus {
  is_active: boolean;
  app_id?: string;
  job_id?: string;
  company?: string;
  title?: string;
  step?: string;
  message?: string;
  level?: string;
  progress_pct?: number;
  is_waiting_for_code?: boolean;
  is_paused?: boolean;
  is_stopped?: boolean;
  browser_active?: boolean;
  browser_is_closed?: boolean;
  active_job?: Record<string, unknown>;
  queue?: Record<string, unknown>;
}

export interface CandidateProfile {
  full_name: string;
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  city: string;
  country: string;
  address: string;
  postal_code: string;
  linkedin_url: string;
  github_url: string;
  portfolio_url: string;
  languages: string;
  current_company: string;
  current_title: string;
  years_of_experience: string;
  education_institution: string;
  education_degree: string;
  work_authorization: string;
  sponsorship_required: string;
  notice_period: string;
  salary_expectation: string;
  willing_to_relocate: string;
  remote_preference: string;
  gender: string;
  veteran_status: string;
  disability_status: string;
  target_roles?: string[];
  target_locations?: string[];
  target_region?: string;
  custom_ai_instructions?: string;
}

export interface PlatformInfo {
  configured: boolean;
  login_url: string;
  status: 'logged_in' | 'not_logged_in';
}

export interface DurableNotification {
  id: number;
  notification_id: string;
  job_id?: string;
  app_id?: string;
  event_id?: number;
  category: string;
  severity: 'info' | 'warning' | 'error' | 'critical';
  title: string;
  message: string;
  url?: string;
  details?: Record<string, unknown>;
  acknowledged: boolean;
  acknowledged_at?: string;
  created_at: string;
}

export interface NotificationListResponse {
  status: string;
  notifications: DurableNotification[];
  unread_count: number;
  total: number;
  latest_id: number;
}

export interface TakeoverStatus {
  status: string;
  is_takeover_active: boolean;
  owner: string | null;
  expires_at: string | null;
  is_paused: boolean;
  is_stopped: boolean;
  read_only: boolean;
}

export interface QueueJobActionResponse {
  status: string;
  job_id?: string;
  message?: string;
  is_paused?: boolean;
  is_stopped?: boolean;
  action?: string;
}

export interface AuthStatusReport {
  profile_dir: string;
  platforms: Record<string, PlatformInfo>;
}

export type ResourceStateStatus = 'idle' | 'loading' | 'ready' | 'error';

export type ResourceKey =
  'stats' | 'applications' | 'tracker' | 'profile' | 'auth' | 'pipeline' | 'automation';

export interface ClassifiedError {
  category: 'network' | 'auth' | 'routing' | 'server' | 'client';
  status: number;
  title: string;
  message: string;
  help?: string;
  rawMessage?: string;
}

export interface ResourceState {
  status: ResourceStateStatus;
  error: ClassifiedError | null;
  lastSuccess: Date | null;
}

export interface ToastNotification {
  visible: boolean;
  message: string;
  type: 'success' | 'error' | 'info' | 'warning';
}

export interface ImportReport {
  imported_applications: number;
  imported_applied: number;
  merged_db_records: number;
  manifest?: Record<string, unknown>;
}

export function classifyHttpError(err: unknown, endpoint = ''): ClassifiedError {
  const errorObj = err as {
    status?: number;
    error?: { detail?: string; message?: string };
    message?: string;
  };
  const status = typeof errorObj?.status === 'number' ? errorObj.status : 0;
  let detail = '';

  if (typeof errorObj?.error?.detail === 'string') {
    detail = errorObj.error.detail;
  } else if (typeof errorObj?.error?.message === 'string') {
    detail = errorObj.error.message;
  } else if (
    typeof errorObj?.message === 'string' &&
    !errorObj.message.includes('Http failure response')
  ) {
    detail = errorObj.message;
  }

  // Sanitize: never expose raw HTML responses, doctypes, or tracebacks
  const detailLower = detail.toLowerCase();
  if (
    detailLower.includes('<html') ||
    detailLower.includes('<!doctype') ||
    detailLower.includes('traceback')
  ) {
    detail = '';
  }

  if (status === 0) {
    return {
      category: 'network',
      status: 0,
      title: 'Backend Unreachable',
      message:
        'Cannot connect to the server. Check your network or ensure the backend service is running.',
      rawMessage: detail,
    };
  }

  if (status === 401 || status === 403) {
    return {
      category: 'auth',
      status,
      title: 'Access Denied',
      message: detail || 'Authentication or proxy permission is required to access this resource.',
      rawMessage: detail,
    };
  }

  if (status === 404) {
    const isSubpath =
      typeof document !== 'undefined' &&
      typeof window !== 'undefined' &&
      Boolean(document.baseURI && document.baseURI !== window.location.origin + '/');

    return {
      category: 'routing',
      status: 404,
      title: 'Endpoint Not Found (404)',
      message: isSubpath
        ? 'The backend endpoint was not found. If running behind a reverse proxy subpath (e.g. /job-applier/), verify that API requests are routed correctly.'
        : detail || 'The requested resource was not found on the server.',
      help: isSubpath
        ? `Reverse proxy routing check recommended${endpoint ? ` for ${endpoint}` : ''}`
        : undefined,
      rawMessage: detail,
    };
  }

  if (status >= 500) {
    return {
      category: 'server',
      status,
      title: `Server Error (${status})`,
      message: detail || 'An internal server error occurred on the backend. Check service logs.',
      rawMessage: detail,
    };
  }

  return {
    category: 'client',
    status,
    title: `Request Failed (${status})`,
    message: detail || 'The request could not be completed.',
    rawMessage: detail,
  };
}
