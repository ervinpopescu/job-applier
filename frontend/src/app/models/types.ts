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
  company?: string;
  title?: string;
  step?: string;
  message?: string;
  level?: string;
  progress_pct?: number;
  is_waiting_for_code?: boolean;
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

export interface AuthStatusReport {
  profile_dir: string;
  platforms: Record<string, PlatformInfo>;
}

export interface AppUser {
  provider: string;
  id: string;
  email: string;
  name: string;
  username: string;
  avatar_url?: string;
}

export interface AppAuthProviders {
  google: boolean;
  github: boolean;
}

export interface AppAuthResponse {
  auth_enabled: boolean;
  authenticated: boolean;
  user: AppUser | null;
  providers: AppAuthProviders;
}

export interface AppAuthState {
  checked: boolean;
  auth_enabled: boolean;
  authenticated: boolean;
  user: AppUser | null;
  providers: AppAuthProviders;
  error: ClassifiedError | null;
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
