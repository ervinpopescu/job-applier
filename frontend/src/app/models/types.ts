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
