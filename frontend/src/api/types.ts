export interface ScenarioMetadata {
  scenario_id: string;
  name: string;
  description: string;
  is_incident: boolean;
  has_sufficient_data: boolean;
  expected_action_eligible: boolean;
  expected_root_cause: string;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  execution_mode: string;
  timestamp?: string;
}

export interface MetricWindow {
  start: string;
  end: string;
}

export interface EvidenceRecord {
  evidence_id: string;
  summary: string;
  computed_at: string;
  source_confidence: string;
  dimension: string | null;
}

export interface IncidentDetails {
  incident_id: string;
  incident_key: string;
  merchant_id: string;
  incident_type: string;
  status: string;
  severity: string;
  detected_at: string;
  window: MetricWindow;
  primary_dimension: string | null;
  primary_dimension_value: string | null;
  metrics: Record<string, any> | null;
  evidence: EvidenceRecord[];
}

export interface InvestigationReport {
  incident_id: string;
  window: MetricWindow;
  investigated_at: string;
  has_sufficient_evidence: boolean;
  has_multiple_concentrations: boolean;
  summary: string;
  primary_findings_count: number;
  secondary_findings_count: number;
}

export interface AgentFinding {
  title: string;
  dimension: string;
  observed_value: string;
  evidence_ref: string | null;
  summary: string;
}

export interface ToolCallRecord {
  call_id?: string;
  tool_name: string;
  arguments?: Record<string, any>;
  success?: boolean;
  result_digest?: string;
}

export interface AgentResponseData {
  incident_id: string;
  reasoning: string;
  verified_facts: string[];
  findings: AgentFinding[];
  uncertainty_or_limitations: string[];
  model_id: string;
  prompt_version: string;
  iterations_count: number;
  tool_calls_used?: ToolCallRecord[];
}

export interface ProposedIntent {
  intent_id: string;
  incident_id: string;
  action: string;
  reason: string;
  target: {
    entity_type: string;
    entity_id: string;
  } | null;
  evidence_refs: string[];
  claimed_amount: {
    amount_paise: number;
    currency: string;
  } | null;
  confidence: string | null;
  parameters: Record<string, any>;
  content_hash: string;
}

export interface VerificationCheck {
  check_id: string;
  name: string;
  passed: boolean;
  expected: string;
  observed: string;
  detail: string;
}

export interface VerificationResult {
  phase: string;
  status: string;
  is_verified: boolean;
  is_rejected: boolean;
  is_inconclusive: boolean;
  summary: string;
  verified_at: string;
  checks_count: number;
  checks: VerificationCheck[];
}

export interface PolicyViolation {
  rule_id: string;
  rule_version: string;
  effect: string;
  message: string;
  detail: string;
}

export interface PolicyDecision {
  decision_id: string;
  intent_id: string;
  intent_hash: string;
  verdict: 'allow' | 'block' | 'escalate';
  authorizes_execution: boolean;
  rationale: string;
  evaluated_at: string;
  expires_at: string;
  rule_set_version: string;
  violations: PolicyViolation[];
  required_approvals: string[];
}

export interface ExecutionResult {
  execution_id: string;
  decision_id: string;
  intent_id: string;
  action: string;
  status: string;
  idempotency_key: string;
  attempted_at: string;
  completed_at: string | null;
  provider_reference: string;
  response_digest: string;
  is_simulation: boolean;
  is_executed: boolean;
  message: string;
  error_code: string | null;
  error_message: string | null;
}

export interface ProcessIncidentResponse {
  run_id: string;
  merchant_id: string;
  status: 'completed' | 'stopped' | 'failed';
  final_stage: 'detection' | 'investigation' | 'agent' | 'verification' | 'policy' | 'execution' | 'completed';
  started_at: string;
  completed_at: string;
  is_completed: boolean;
  is_simulated: boolean;
  is_stopped: boolean;
  is_failed: boolean;
  summary: string;
  stop_reason: string | null;
  incident: IncidentDetails | null;
  investigation_report: InvestigationReport | null;
  agent_response: AgentResponseData | null;
  proposed_intent: ProposedIntent | null;
  verification_result: VerificationResult | null;
  policy_decision: PolicyDecision | null;
  execution_result: ExecutionResult | null;
  scenario_classification?: {
    scenario_id: string;
    confidence: number;
    rationale: string;
    is_incident: boolean;
    is_action_eligible: boolean;
  } | null;
}

export interface AuditEvent {
  event_id: string;
  sequence: number;
  occurred_at: string;
  actor: string;
  event_type: string;
  summary: string;
  incident_id: string | null;
  subject_id: string | null;
  payload: Record<string, any>;
  payload_digest: string;
}

export interface AuditTrailResponse {
  events: AuditEvent[];
  count: number;
  is_valid: boolean;
  verification_errors: string[];
}

export type StageId =
  | 'detection'
  | 'investigation'
  | 'agent'
  | 'verification'
  | 'policy'
  | 'execution';

export interface StageProgressEvent {
  run_id: string;
  stage: StageId | 'pipeline';
  status: 'running' | 'completed' | 'blocked' | 'failed' | 'stopped';
  timestamp: string;
  details?: string | null;
  payload?: ProcessIncidentResponse | null;
}

export interface StageExecutionTiming {
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  status: 'waiting' | 'running' | 'completed' | 'blocked' | 'failed' | 'skipped' | 'duplicate';
  details?: string | null;
}

export interface IncidentJob {
  job_id: string;
  incident_id: string;
  merchant_id: string;
  source: string;
  event_id: string;
  event_type: string;
  payment_id: string;
  status: 'received' | 'queued' | 'processing' | 'completed' | 'failed' | 'escalated';
  attempt_count: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  payload?: Record<string, any>;
  pipeline_result?: ProcessIncidentResponse | null;
}

