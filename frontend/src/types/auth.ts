import type { EmployeeProfile } from './domain';

export interface AuthUser {
  id?: string | null;
  email: string;
  display_name?: string | null;
  role: string;
}

export interface AuthResponse {
  success: boolean;
  message?: string | null;
  user?: AuthUser | null;
}

export interface AuthMeResponse {
  authenticated: boolean;
  user?: AuthUser | null;
}

export interface AdminAccount {
  email: string;
  display_name?: string | null;
  role: string;
  whitelist_enabled: boolean;
  registered: boolean;
  is_active: boolean;
}

export interface AdminAccountsResponse {
  items: AdminAccount[];
}

export interface AdminExportConversation {
  session_id: string;
  user_email: string;
  user_display_name?: string | null;
  employee_name?: string | null;
  intent_id?: string | null;
  stage?: string | null;
  session_created_at: string;
  conversation_started_at?: string | null;
  conversation_ended_at?: string | null;
  turn_count: number;
  has_conversation: boolean;
  has_rehearsal: boolean;
}

export interface AdminExportConversationListResponse {
  items: AdminExportConversation[];
}

export interface AdminUsagePerformanceItem {
  goal: string;
  current_performance: string;
}

export interface AdminUsageIntent {
  intent_id?: string | null;
  name?: string | null;
  performance_context?: string | null;
  performance_items?: AdminUsagePerformanceItem[];
}

export interface AdminUsagePersonality {
  openness?: number | null;
  conscientiousness?: number | null;
  extraversion?: number | null;
  agreeableness?: number | null;
  neuroticism?: number | null;
}

export interface AdminUsageMotivation {
  primary_motive_id?: string | null;
  secondary_motive_ids?: string[];
}

export interface AdminUsageConversationTurn {
  turn_index?: number | null;
  speaker: string;
  text: string;
  created_at?: string | null;
}

export interface AdminUsageGuidanceGroup {
  title?: string | null;
  summary?: string | null;
  details?: string[];
}

export interface AdminUsageGuidance {
  purpose?: string | null;
  opening_suggestion?: string | null;
  risk_preview?: string[];
  response_strategies?: string[];
  safer_phrases?: string[];
  dimension_points?: Record<string, AdminUsageGuidanceGroup[]>;
}

export interface AdminUsageRisk {
  category?: string | null;
  explanation?: string | null;
  safer_phrase?: string | null;
}

export interface AdminUsageBetterPhrase {
  diagnostic_dimension_id?: string | null;
  original?: string | null;
  suggestion?: string | null;
  reason?: string | null;
}

export interface AdminUsageCareerElementAdvice {
  element?: string | null;
  suggestion?: string | null;
  reason?: string | null;
}

export interface AdminUsageCoachTask {
  task_id: string;
  task_name: string;
  status: string;
  score?: number | null;
  summary?: string | null;
  basis?: string[];
  strengths?: string[];
  improvement_points?: string[];
  risks?: Array<AdminUsageRisk | string>;
  better_phrases?: Array<AdminUsageBetterPhrase | string>;
  career_elements_advice?: Array<AdminUsageCareerElementAdvice | string>;
}

export interface AdminUsageSessionDetail {
  read_only: boolean;
  session_id: string;
  user_email: string;
  user_display_name?: string | null;
  stage?: string | null;
  run_mode?: string | null;
  locale?: 'zh-CN' | 'en' | 'de' | 'ja' | null;
  session_created_at: string;
  session_updated_at?: string | null;
  rehearsal_ended_at?: string | null;
  employee_profile?: EmployeeProfile | null;
  supplemental_info?: string | null;
  intent?: AdminUsageIntent | null;
  personality?: AdminUsagePersonality | null;
  motivation?: AdminUsageMotivation | null;
  runtime_notes?: string[];
  conversation?: AdminUsageConversationTurn[];
  guidance?: AdminUsageGuidance | null;
  coach_tasks?: AdminUsageCoachTask[];
}
