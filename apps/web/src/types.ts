export type TraceReference = { trace_id: string; trace_url: string };
export type RuntimeConfiguration = { status: 'available' | 'unavailable'; environment: { name: string; value: string }[]; mounts: { destination: string; type: string; read_only: boolean }[]; truncated: boolean };
export type DeploymentContext = { deployment_id: string; collector_id: string; project: string; service: string; container_id: string; environment: string; revision: string | null; version: string | null; source_url: string | null; image_ref: string | null; image_id: string | null; repo_digests: string[]; runtime_config?: RuntimeConfiguration | null };
export type DeploymentObservation = DeploymentContext & { repository_id: string | null; first_observed: string; last_observed: string };
export type DeploymentPage = { items: DeploymentObservation[]; total: number; page: number; page_size: number };
export type SourceResolution = { status: 'pending' | 'matched' | 'unavailable' | 'unknown'; requested_commit: string | null; analyzed_commit: string | null; reason: string | null };
export type DeploymentEvidence = { deployment_context?: DeploymentContext | null; source_resolution?: SourceResolution };
export type Candidate = { file_path: string; symbol_name: string | null; service_name: string | null; score: number; reasons: string[]; snippet: string | null; line_start: number | null; line_end: number | null; group_id?: string | null; event_ids?: string[] };
export type Hypothesis = { id?: string | null; action_plan?: HypothesisActionPlan | null; rank: number; failure_layer?: 'application' | 'database' | 'network' | 'infrastructure' | 'unknown'; title: string; explanation: string; confidence: number | null; confidence_status?: 'unavailable' | 'uncalibrated' | 'calibrated'; model_score?: number | null; evidence: string[]; evidence_refs?: string[]; contradicting_evidence_refs?: string[]; next_checks: string[] };
export type RuntimeProvenance = { collector_id: string; project: string; environment: string; service: string; container_ids: string[]; requested_start: string; requested_end: string; observed_at: string; actual_start?: string | null; actual_end?: string | null; coverage: string };
export type RuntimeCollection = { id: string; capability: string; status: string; reason?: string | null; duration_ms: number };
export type EvidenceItem = { id: string; kind: string; repository?: string | null; revision?: string | null; runtime?: RuntimeProvenance | null; file_path?: string | null; line_start?: number | null; line_end?: number | null; content: string; data: Record<string, unknown>; limitations: string[]; request_id?: string | null; truncated: boolean };
export type InvestigationSummary = { mode: string; version: number; rounds: number; model_calls: number; tool_calls: number; elapsed_ms: number; stop_reason: string; incomplete: boolean; unresolved_gaps: string[] };
export type InvestigationEvent = { id: string; kind: string; tool?: string | null; question?: string | null; status: string; reason?: string | null; evidence_ids: string[]; duration_ms: number };
export type ValidationIssue = { path: (string | number)[]; code: string; message: string };
export type AnalysisValidationError = { stage: 'hypothesis_generation' | 'investigation_assessment' | 'evidence_retrieval' | 'diagnosis_review'; kind: 'json' | 'schema' | 'semantic' | 'limit'; model_call?: number | null; request_id?: string | null; issues: ValidationIssue[]; total_issues: number };
export type GuardrailEvent = { id: string; stage: 'input' | 'output'; status: 'pending' | 'passed' | 'flagged' | 'error' | 'skipped'; code: string; model_call: number; duration_ms: number };
export type GuardrailSummary = { mode: 'off' | 'observe' | 'enforce'; policy_version: string; checks: number; model_calls: number; duration_ms: number; events: GuardrailEvent[] };
export type ReasoningSummary = { mode: 'baseline' | 'structured' | 'structured_review'; prompt_version: string; review_status: 'disabled' | 'not_needed' | 'pending' | 'completed' | 'failed' | 'skipped'; review_reasons: string[]; review_failure: string | null; model_calls: number };
export type InvestigationEvidence = { reasoning?: ReasoningSummary | null; runtime_collection?: RuntimeCollection[]; runtime_evidence?: EvidenceItem[]; retrieved_evidence?: EvidenceItem[]; investigation?: InvestigationSummary | null; investigation_events?: InvestigationEvent[]; validation_errors?: AnalysisValidationError[]; guardrails?: GuardrailSummary | null };
export type GraphContext = { provider: string; provider_version: string | null; repository: string; repo_revision: string | null; indexed_revision: string | null; status: string; reason: string | null; completeness: string; retrieval_ms: number; symbols: { uid: string; name: string; file_path: string; line_start: number | null }[]; relationships: { source_uid: string; target_uid: string; relationship: string }[]; seeds: { file_path: string; symbol_name: string; status: string; reason: string | null }[] };
export type Report = { observability?: TraceReference | null } & InvestigationEvidence & DeploymentEvidence & { action_plan_version?: 1 | null; runbooks?: RunbookSnapshot[]; recommendation_limitations?: string[]; follow_up?: ActionEvent[]; repository_source?: RepositorySource | null; incident_id: string; likely_service: string | null; likely_error_class: string | null; candidates: Candidate[]; hypotheses: Hypothesis[]; diagnosis_status?: 'provisional' | 'insufficient_evidence' | 'unknown'; diagnosis_reason?: string | null; next_checks?: string[]; summary: string; engine: string; fallback_reason?: string | null; graph_context?: GraphContext | null; incident_context?: IncidentContext | null };
export type StageEvent = { stage: string; started_at: string; completed_at: string | null; attempt: number };
export type StackFrame = { raw: string; file_path: string | null; function_name: string | null; line_number: number | null };
export type LogException = { id: string; type: string; message: string; relation: 'caused_by' | 'during_handling' | 'cause' | null; previous_id: string | null; stack_frames: StackFrame[] };
export type LogEvent = { id: string; line_start: number; line_end: number; message: string; service: string | null; timestamp: string | null; timestamp_utc: string | null; trace_id: string | null; request_id: string | null; error_bearing: boolean; exceptions: LogException[]; stack_frames: StackFrame[]; limitations: string[] };
export type IncidentGroup = { id: string; event_ids: string[]; services: string[]; error_bearing: boolean; first_line: number; grouping_basis: 'trace_id' | 'request_id' | 'record'; max_candidate_score: number };
export type IncidentContext = { version: 1; events: LogEvent[]; groups: IncidentGroup[]; primary_group_id: string | null; selection_reason: string | null; limitations: string[] };
export type ParsedLog = { services: string[]; exception_types: string[]; error_lines: string[]; keywords: string[]; stack_frames: StackFrame[]; raw_text: string; incident_context?: IncidentContext | null };
export type IndexSummary = { indexed_files: number; indexed_symbols: number; service_mappings: { service_name: string; image: string | null; build_context: string | null }[]; repo_root: string };
export type Incident = DeploymentEvidence & { observability?: TraceReference | null; error_batch_id?: string | null; error_tracking_status?: 'untracked' | 'tracked' | 'duplicate'; repository_source?: RepositorySource | null; id: string; title: string; repository_id: string; repository_name: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'canceled'; created_at: string; updated_at: string; completed_at: string | null; config_revision: string; stage: string; timeline: StageEvent[]; report: Report | null; evidence: InvestigationEvidence & { raw_log?: string; parsed_log?: ParsedLog; candidates?: Candidate[]; deep_analysis?: Record<string, unknown>; index_summary?: IndexSummary; graph_context?: GraphContext | null; graph_preparation?: GraphPreparation | null }; error: string | null; connection_error: string | null };
export type GraphPreparation = { repository: string; status: 'disabled' | 'external' | 'not_prepared' | 'queued' | 'preparing' | 'ready' | 'stale' | 'unavailable'; source_revision: string | null; indexed_revision: string | null; indexed_at: string | null; reason: string | null };
export type IncidentPage = { items: Incident[]; total: number; page: number; page_size: number };
export type Validation = { valid: boolean; message: string; services: { service_name: string; image: string | null; build_context: string | null }[] };
export type Repository = { source_type?: 'local' | 'github'; remote_url?: string | null; branch?: string | null; token_configured?: boolean; synced_revision?: string | null; last_synced_at?: string | null; last_checked_at?: string | null; id: string; name: string; repo_root: string | null; compose_path: string | null; archived: boolean; created_at: string; validation: Validation | null; index_summary: IndexSummary | null };
export type OverviewData = { total: number; completed: number; failed: number; active: number; average_duration_seconds: number; activity: { date: string; incidents: number }[]; services: { name: string; count: number }[]; recent: Incident[] };
export type SettingsData = { saved: Record<string, string | number | boolean>; active: Record<string, string | number | boolean>; overridden: string[]; restart_required: string[]; infrastructure_fields: string[]; control_db_path: string; secret_configured: boolean; graph_secret_configured?: boolean };
export const isActive = (run: Incident) => run.status === 'running' || run.status === 'queued';

export type IssueStatus = 'open' | 'acknowledged' | 'investigating' | 'resolved' | 'ignored';
export type ErrorDimension = 'repository' | 'service' | 'error_class' | 'file' | 'function';
export type Issue = {
  id: string; repository_id: string; repository_name: string; title: string; owner: string;
  status: IssueStatus; resolution_note: string; resolved_at: string | null; created_at: string; updated_at: string;
  first_seen: string | null; last_seen: string | null; occurrence_count: number; previous_count: number;
  total_count: number; change: number; change_percent: number | null; source_timestamp_count: number;
  fallback_timestamp_count: number; redirected_from?: string | null; tracking_since?: string;
};
export type IssuePage = { items: Issue[]; total: number; page: number; page_size: number; start_at: string; end_at: string; tracking_since: string };
export type ErrorOccurrence = {
  id: string; issue_id: string; batch_id: string; event_id: string; effective_at: string;
  timestamp_source: 'event' | 'submission'; event: LogEvent & { group_id?: string | null; deployment_context?: DeploymentContext | null };
  investigations: { id: string; title: string }[];
};
export type OccurrencePage = { items: ErrorOccurrence[]; total: number; page: number; page_size: number };
export type IssueActivity = { id: string; issue_id: string; kind: string; created_at: string; effective_at: string; details: Record<string, unknown> };
export type ActivityPage = { items: IssueActivity[]; total: number; page: number; page_size: number };
export type ErrorIntelligenceData = {
  tracking_since: string; start_at: string; end_at: string; previous_start_at: string; dimension: ErrorDimension;
  summary: { occurrences: number; previous_occurrences: number; change: number; change_percent: number | null;
    issues: number; active_issues: number; new_issues: number; reopened_issues: number;
    source_timestamp_count: number; fallback_timestamp_count: number };
  trends: { date: string; occurrences: number }[];
  hotspots: { value: string; label: string; occurrences: number }[];
  frequent_issues: Issue[]; growing_issues: Issue[];
};

export type RepositorySource = { source_type: string; remote_url: string; branch: string; commit: string };
export type RepositorySync = { id: string | null; repository_id: string; status: 'idle' | 'queued' | 'running' | 'completed' | 'failed'; stage: string; result?: 'updated' | 'unchanged' | null; revision?: string | null; completed_at?: string | null; error?: string | null; connection_error?: string | null; graph_preparation?: GraphPreparation | null };

export type NextCheck = {
  id: string; instruction: string; context: string; command: string | null; placeholders: string[];
  evidence_to_collect: string; supports_hypothesis: string; weakens_hypothesis: string;
};
export type MitigationProposal = {
  id: string; title: string; intended_effect: string; prerequisites: string[]; missing_prerequisites: string[];
  risks: string[]; affected_services: string[]; steps: string[]; rollback: string[]; runbook_ids: string[];
  verification: { measure: string; success_condition: string; observation_period: string };
};
export type HypothesisActionPlan = {
  version: 1; supporting_evidence: { claim: string; evidence_refs: string[] }[]; missing_evidence: string[];
  checks: NextCheck[]; runbooks: { runbook_id: string; section: string; reason: string }[];
  mitigations: MitigationProposal[];
};
export type RunbookSnapshot = {
  id: string; title: string; file_path: string; revision: string | null; content_hash: string;
  services: string[]; error_classes: string[]; failure_layers: string[];
  sections: { heading: string; line_start: number; line_end: number; content: string }[];
};
export type ActionStatus = 'supports' | 'weakens' | 'inconclusive' | 'blocked' | 'skipped' |
  'in_progress' | 'applied' | 'verified' | 'failed' | 'rolled_back';
export type ActionEvent = {
  client_event_id: string; incident_id: string; action_id: string; action_kind: 'check' | 'mitigation';
  status: ActionStatus; observations: string; responder: string | null; useful?: boolean | null; recorded_at: string;
};
