export type StepKey = 'profile' | 'intent' | 'simulation' | 'guidance' | 'rehearsal' | 'report';

export type SessionLocale = 'zh-CN' | 'en' | 'de' | 'ja';

export type SessionStage = 'created' | 'profile_ready' | 'setup_ready' | 'guidance_ready' | 'rehearsal' | 'report_ready' | 'ended';

export interface EmployeeProfile {
  employee_id?: string | null;
  name?: string | null;
  employee_alias?: string | null;
  role?: string | null;
  department?: string | null;
  level?: string | null;
  reporting_line?: string | null;
  performance_rating?: string | null;
  tcl?: string | null;
  review_cycle?: string | null;
  conversation_topic?: string | null;
  key_goals?: unknown[] | string | null;
  facts?: unknown[] | string | null;
  past_ratings?: unknown[] | string | null;
  historical_feedback?: unknown[] | string | null;
  management_actions?: unknown[] | string | null;
  employee_status_summary?: string | null;
  current_career_elements?: string[] | null;
  sensitive_constraints?: Record<string, { status?: string | null } | string | null> | null;
  source_profile_text?: string | null;
  supplemental_info?: string | null;
  [key: string]: unknown;
}

export interface EmployeeRecord {
  employee_id?: string | null;
  name?: string | null;
  employee_alias?: string | null;
  role?: string | null;
  department?: string | null;
  manager?: string | null;
  profile?: EmployeeProfile | null;
  profile_text?: string | null;
  [key: string]: unknown;
}

export interface EmployeeSearchResponse {
  database?: string;
  items: EmployeeRecord[];
}

export interface IntentOption {
  id: string;
  name?: string;
  description?: string | null;
  eligibility?: IntentEligibilityRule | null;
  [key: string]: unknown;
}

export interface IntentEligibilityRule {
  performance_ratings?: number[];
  tcl_values?: string[];
  match_mode?: 'any';
}

export interface BigFivePersonality {
  openness: number;
  conscientiousness: number;
  extraversion: number;
  agreeableness: number;
  neuroticism: number;
}

export interface MotiveOption {
  id: string;
  name?: string;
  dimension?: string;
  description?: string;
  examples?: string[];
  [key: string]: unknown;
}

export interface VADVector {
  valence: number;
  arousal: number;
  dominance: number;
}

export interface EmotionAnchor {
  id: string;
  name?: string;
  description?: string;
  vad: VADVector;
  [key: string]: unknown;
}

export interface MotiveRecommendation {
  primary_motive_id?: string;
  secondary_motive_ids?: string[];
}

export interface TtsSetupOptions {
  enabled: boolean;
  default_voice: string;
  voices: string[];
  sample_rate: number;
}

export interface RehearsalSpeechRequest {
  enabled: boolean;
  voice?: string | null;
  stream_id?: string | null;
}

export interface SetupOptions {
  intents: IntentOption[];
  default_intent?: string | null;
  motives: MotiveOption[];
  emotion_anchors: EmotionAnchor[];
  default_big_five?: BigFivePersonality | null;
  motive_recommendations?: Record<string, MotiveRecommendation>;
  default_motive_recommendation?: MotiveRecommendation | null;
  speech?: TtsSetupOptions;
}

export type AdminTestWorkflowDestination = 'rehearsal' | 'report';

export interface AdminTestConversationTurn {
  speaker: 'manager' | 'employee';
  text: string;
}

export interface AdminTestWorkflowCreatePayload {
  locale?: SessionLocale;
  destination: AdminTestWorkflowDestination;
  intent_id: string;
  personality: BigFivePersonality;
  primary_motive_id: string;
  secondary_motive_ids: string[];
  conversation: AdminTestConversationTurn[];
}

export interface IntentGoalPerformanceItem {
  goal: string;
  current_performance: string;
  generation_reason?: string | null;
}

export interface IntentResult {
  intent_id?: string;
  id?: string;
  name?: string;
  performance_locale?: SessionLocale | null;
  performance_context?: string | null;
  performance_items?: IntentGoalPerformanceItem[];
  [key: string]: unknown;
}

export interface IntentPerformanceDraftResponse {
  intent_id: string;
  locale: SessionLocale;
  performance_context: string;
  performance_items: IntentGoalPerformanceItem[];
}

export interface EmployeeLatestSetupSettingsResponse {
  employee_id: string;
  found: boolean;
  supplemental_info?: string | null;
  personality?: BigFivePersonality | null;
  primary_motive_id?: string | null;
  secondary_motive_ids?: string[];
  updated_at?: string | null;
}

export interface EmotionTurnSnapshot {
  valence: number;
  arousal: number;
  dominance: number;
  anchor_id?: string | null;
  reason_summary?: string | null;
}

export interface RehearsalStageTiming {
  name: string;
  start_offset_ms: number;
  end_offset_ms: number;
  duration_ms: number;
  outcome: string;
  parallel_group?: string;
}

export interface RehearsalTurnTiming {
  schema_version: number;
  attempt_id: string;
  transport: string;
  speech_enabled: boolean;
  explicit_thinking_enabled: boolean;
  explicit_thinking_ms: number | null;
  manager_turn_index: number | null;
  employee_turn_index: number | null;
  outcome: string;
  milestones_ms: Record<string, number>;
  summary_ms: Record<string, number>;
  stages: RehearsalStageTiming[];
}

export interface RehearsalClientTiming {
  submit_to_start_ms?: number;
  submit_to_first_visible_reply_ms?: number;
  visible_reply_stream_ms?: number;
  submit_to_done_ms: number;
  recovered: boolean;
  transport: 'stream' | 'refreshed_after_stream' | 'nonstream_fallback';
}

export interface ConversationTurn {
  turn_index?: number;
  speaker?: string;
  text?: string;
  metadata?: Record<string, unknown> & {
    emotion_snapshot?: EmotionTurnSnapshot;
    rehearsal_timing?: RehearsalTurnTiming;
    rehearsal_client_timing?: RehearsalClientTiming;
  };
  [key: string]: unknown;
}

export type RehearsalDimensionId = 'start' | 'emotion' | 'requirement' | 'plan';

export interface RehearsalRuntimeContext {
  runtime_notes?: string[];
  speech_voice?: string | null;
  covered_dimensions?: RehearsalDimensionId[];
  updated_at?: string;
  [key: string]: unknown;
}

export interface RehearsalContextUpdatePayload {
  runtime_note?: string | null;
  runtime_notes?: string[] | string | null;
  clear_context?: boolean;
}

export interface MotivationState {
  primary_motive_id?: string | null;
  secondary_motive_ids?: string[];
  primary_score?: number;
  secondary_scores?: Record<string, number>;
  total_satisfaction?: number;
  last_change_reason?: string | null;
  has_manager_response?: boolean;
  updated_at?: string;
}

export interface EmotionState {
  current_vad?: VADVector;
  current_anchor_id?: string | null;
  transition_strategy?: 'expected_value' | 'maximum_probability' | 'sampling';
  last_reason_summary?: string | null;
  reply_emotion_guidance?: string | null;
  has_manager_response?: boolean;
  updated_at?: string;
}

export interface SessionState {
  session_id: string;
  stage: SessionStage;
  /** Missing only on legacy cached payloads; the server treats it as zh-CN. */
  locale?: SessionLocale;
  run_mode?: string;
  supplemental_info?: string | null;
  employee_profile?: EmployeeProfile | null;
  intent?: IntentResult | null;
  personality?: BigFivePersonality | null;
  motivation?: MotivationState | null;
  emotion_state?: EmotionState | null;
  setup_ready?: boolean;
  guidance_report_id?: string | null;
  coach_report_id?: string | null;
  rehearsal_ended_at?: string | null;
  rehearsal_context?: RehearsalRuntimeContext | null;
  conversation?: ConversationTurn[];
  user_turn_count?: number;
  warnings?: string[];
  [key: string]: unknown;
}

export interface DocumentRecord {
  document_id?: string;
  parsed_text?: string;
  profile?: EmployeeProfile | null;
  [key: string]: unknown;
}

export interface GuidancePointGroup {
  title: string;
  summary?: string | null;
  details: string[];
  summary_knowledge_chunk_ids?: string[];
  detail_knowledge_chunk_ids?: string[][];
}

export interface KnowledgeCitationAnchor {
  target: string;
  highlight_text: string;
  source_quote: string;
  source_context: string;
}

export interface KnowledgeCitation {
  chunk_id?: string | null;
  source_id?: string | null;
  title?: string | null;
  scope?: string | null;
  quote?: string | null;
  targets?: string[];
  source_type?: 'knowledge_base' | 'skill' | null;
  anchors?: KnowledgeCitationAnchor[] | null;
}

export interface GuidanceDimensionPoints {
  start: GuidancePointGroup[];
  emotion: GuidancePointGroup[];
  requirement: GuidancePointGroup[];
  plan: GuidancePointGroup[];
}

export interface GuidanceReport {
  session_id?: string;
  locale: SessionLocale;
  intent_id?: string;
  guidance_version?: string | null;
  culture_version?: string | null;
  primary_motive_id?: string | null;
  secondary_motive_ids?: string[];
  purpose?: string | null;
  opening_suggestion?: string | null;
  risk_preview?: string[] | null;
  response_strategies?: string[] | null;
  safer_phrases?: string[] | null;
  dimension_points?: GuidanceDimensionPoints | null;
  citations?: KnowledgeCitation[];
  disclaimer?: string | null;
  [key: string]: unknown;
}

export type WorkflowStreamStatus = 'idle' | 'streaming' | 'ready' | 'partial_error';
export type DraftSectionStatus = 'idle' | 'generating' | 'done' | 'error';
export type CoachTaskStatus = 'idle' | 'running' | 'done' | 'error';

export type GuidanceSectionKey = 'purpose' | 'opening_suggestion' | 'risk_preview' | 'response_strategies' | 'safer_phrases';

export interface GuidanceSectionDraft {
  key: GuidanceSectionKey;
  title: string;
  text: string;
  items: string[] | null;
  point_groups: GuidancePointGroup[] | null;
  status: DraftSectionStatus;
  error?: string | null;
}

export interface CoachReport {
  session_id: string;
  locale: SessionLocale;
  coach_version?: string | null;
  status?: string;
  task_results: CoachTaskResult[];
  disclaimer?: string;
}

export type CoachTaskResultStatus = 'success' | 'insufficient_information' | 'failed';

export interface CoachDimensionScore {
  id: string;
  name: string;
  score?: number | null;
  level?: string | null;
  basis?: string | null;
  comment?: string | null;
}

export interface CoachRiskItem {
  category?: string;
  explanation: string;
  matched_text?: string | null;
}

export interface CoachBetterPhrase {
  diagnostic_dimension_id?: string | null;
  original?: string | null;
  suggestion: string;
  reason: string;
}

export interface CoachCareerElementAdvice {
  element: string;
  suggestion: string;
  reason: string;
}

export interface CoachTaskResult {
  task_id: string;
  task_name: string;
  status: CoachTaskResultStatus;
  score?: number | null;
  summary: string;
  strengths?: string[];
  dimension_scores?: CoachDimensionScore[];
  improvement_points?: string[];
  risks?: CoachRiskItem[];
  better_phrases?: CoachBetterPhrase[];
  career_elements_advice?: CoachCareerElementAdvice[];
  citations?: KnowledgeCitation[];
}

export interface CoachTaskDraft {
  task_id: string;
  task_name: string;
  status: CoachTaskStatus;
  result?: CoachTaskResult | null;
  summary?: string;
  score?: number | null;
  error?: string | null;
}

export interface AsrTranscribeResponse {
  text: string;
  audio_emotion?: string | null;
  duration_seconds?: number | null;
  provider: string;
}

export type AsrReadinessStatus = 'warming' | 'ready' | 'degraded';

export interface AsrReadinessResponse {
  status: AsrReadinessStatus;
  model: string;
  provider?: 'local' | 'bosch' | 'browser';
  active_preview_sessions: number;
  /** Zero means preview sessions are admitted without a user-count cap. */
  max_preview_sessions: number;
  max_inference_batch_size?: number;
  admission_mode?: 'bounded' | 'unbounded';
  queue_depth?: number;
  window_seconds?: number;
  detail?: string;
  streaming_mode?: 'browser_native' | 'chunked_batch' | 'native' | 'unknown' | string;
  stream_chunk_seconds?: number;
  first_stream_chunk_seconds?: number;
}

export interface StreamEvent<T = Record<string, unknown>> {
  event: string;
  data: T;
}
