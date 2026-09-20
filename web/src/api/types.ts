// 与 open_story_engine/api_models.py 对齐的类型；外层 snake_case，核心对象保留 camelCase。

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue }

export interface PreviewCapability {
  available: boolean
  code: string | null
  message: string | null
}

export interface PackageSummary {
  package_id: string
  version: string
  title: string
  summary: string
  visual_prompt?: string
  modular: boolean
  module_index_sha256?: string | null
  beat_count: number
  character_count: number
  context_preview: PreviewCapability
}

export interface PackageIssue {
  package_id: string
  version: string
  code: string
  message: string
}

export interface PackageList {
  packages: PackageSummary[]
  issues: PackageIssue[]
}

export interface PublishedScene {
  id: string
  url: string
  alt: string
  source: 'published'
}

export interface EntrySummary {
  opening_image?: PublishedScene | null
  id: string
  title: string
  chapter_title: string
  chapter_id: string | null
  beat_id: string
  node_id: string
  summary: string
  source_character_ids: string[]
  available_to_new_character?: boolean
}

export interface ProfileField {
  id: string
  label: string
  type: 'text' | 'integer'
  minLength?: number
  maxLength?: number
  minimum?: number
  maximum?: number
  [key: string]: unknown
}

export interface NewCharacterOption {
  enabled: boolean
  profile_fields: ProfileField[]
}

export interface BeatSummary {
  id: string
  node_id: string
  summary: string
}

export interface EntityCard {
  id: string
  name?: string | null
  description?: string | null
  menuDescription?: string | null
  role?: string | null
  tags?: string[] | null
  roleGroup?: string | null
  identitySummary?: string | null
  motivation?: string | null
  openingHook?: string | null
  defaultEntryPointId?: string | null
  portraitAsset?: string | null
  exits?: string[] | null
  portable?: boolean | null
  [key: string]: unknown
}

export interface LedgerSource {
  kind: string
  ref: string
  nodeRef?: string | null
  [key: string]: unknown
}

export interface LedgerEntry {
  id: string
  sequence: number
  kind: 'location' | 'character' | 'item' | 'relationship' | 'clue' | 'event'
  operation: 'inherited' | 'added' | 'changed'
  entityId: string
  summary: string
  before: JsonValue
  after: JsonValue
  source: LedgerSource
  [key: string]: unknown
}

export interface BranchLedger {
  schemaVersion: 'branch-state-ledger/0.1'
  entries: LedgerEntry[]
  [key: string]: unknown
}

export interface StoryState {
  currentNodeId?: string | null
  currentLocationId?: string | null
  playerLocationId?: string | null
  playerCharacterId?: string | null
  characterLocationIds?: Record<string, string> | null
  branchLedger?: BranchLedger | null
  [key: string]: unknown
}

export interface DirectionView {
  id: string
  title: string
  summary?: string | null
  [key: string]: unknown
}

export interface BranchView {
  id: string
  sessionId: string
  sequence: number
  parentId?: string | null
  kind: string
  sourceNodeRef: string
  narrativeText: string
  summary: string
  branchState?: StoryState | null
  nextDirections: DirectionView[]
  openThreads: string[]
  openingActions?: { title: string; summary: string }[]
  readerChoices?: { id: string; title: string; summary: string; evidence: string[] }[]
  createdAt: string
  canonicalRelation?: string | null
  [key: string]: unknown
}

export interface BranchSummaryItem {
  id: string
  session_id: string
  parent_id: string | null
  sequence: number
  created_at: string
  action?: string | null
}

export interface BranchPage {
  session_id: string
  branches: BranchSummaryItem[]
  next_after_sequence: number | null
}

export interface SessionRecord {
  id: string
  storyPackageId: string
  storyPackageVersion: string
  storyPackageModuleIndexSha256?: string | null
  stateVersion: number
  status: string
  currentState: StoryState
  [key: string]: unknown
}

export interface ChapterView {
  id: string
  title: string
  [key: string]: unknown
}

export interface ContextBeat {
  id: string
  summary: string
  [key: string]: unknown
}

export interface CharacterDetail {
  name: string
  detail: string
  [key: string]: unknown
}

export interface LineRange {
  start: number
  end: number
}

export interface NarrativeCue {
  text: string
  evidenceParagraphId?: string | null
  lineRange?: LineRange | null
  [key: string]: unknown
}

export interface ContextData {
  world: Record<string, JsonValue>
  currentChapter: ChapterView
  currentBeat: ContextBeat
  narrativeBrief: NarrativeCue[]
  priorNarrativeBrief: NarrativeCue[]
  previousSourceLine: number
  actionContract: Record<string, JsonValue>
  modulePaths: string[]
  previousBeatSummaries: string[]
  characters: EntityCard[]
  locations: EntityCard[]
  items: EntityCard[]
  characterDetails: CharacterDetail[]
  characterIdentityEvidence: Record<string, JsonValue>[]
  sourceDialogueContext: Record<string, JsonValue>[]
  continuityText: string
  [key: string]: unknown
}

export interface PackageCatalog {
  supporting_characters?: EntityCard[]
  package: PackageSummary
  new_character?: NewCharacterOption | null
  entries: EntrySummary[]
  beats: BeatSummary[]
  characters: EntityCard[]
  locations: EntityCard[]
  items: EntityCard[]
}

export interface SessionSummary {
  title?: string | null
  role_name?: string | null
  updated_at?: string | null
  recent_progress?: string | null
  id: string
  package_id: string
  version: string
  status: string
  state_version: number
  created_at?: string | null
}

export interface SessionList {
  available: boolean
  sessions: SessionSummary[]
}

export interface SessionPreviewCapability extends PreviewCapability {
  binding_status: 'matched' | 'legacy_unverified' | 'changed' | 'unavailable'
}

export interface SessionView {
  session: SessionRecord
  branches: BranchView[]
  branches_included: boolean
  context_preview: SessionPreviewCapability
}

export interface StateView {
  session_id: string
  branch_id: string | null
  state_version: number
  state: StoryState
  ledger: BranchLedger
}

export interface ContextView {
  mode: 'entry_preview' | 'branch_preview'
  package: PackageSummary
  session_id: string | null
  parent_branch_id: string | null
  context_sha256: string
  state: StoryState
  context: ContextData
}

export interface ContextRequest {
  package?: { package_id: string; version: string }
  entry_point_id?: string
  source_character_id?: string
  session_id?: string
  parent_branch_id?: string
}

export interface HealthResponse {
  status: 'ok'
  phase: 'read_only' | 'play'
  generation_available: boolean
  state_updates_available: boolean
}

export interface PlayCreateRequest {
  package: { package_id: string; version: string }
  entry_point_id: string
  source_character_id?: string
  new_character?: Record<string, string | number>
  identity_opening?: boolean
  request_id?: string
}

export interface PlayCreateResponse {
  session: SessionRecord
  branch: BranchView
}

export interface PreparedChoice {
  image_prefetch_url?: string
  id: string
  title: string
  summary: string
  draft_id: string
  status: 'queued' | 'generating' | 'validating' | 'ready' | 'failed' | 'expired'
}

export interface PlayContinueRequest {
  history_id?: string
  draft_id?: string
  choice_id?: string
  subscriber_id?: string
  parent_branch_id: string
  direction_id?: string
  text?: string
  request_id?: string
}

export interface PlayContinueResponse {
  status: 'written' | 'rejected'
  request_id: string
  deduplicated: boolean
  branch?: BranchView | null
  kind?: string | null
  reason?: string | null
}

export interface SourceChapterView {
  session_id: string
  branch_id: string
  chapter_id: string
  title: string
  text: string
  source_progress: string
}

export interface Journey {
  branch_id: string
  threads: JourneyThread[]
  goals: JourneyGoal[]
  ledger_coverage: { goals: 'recorded' | 'unknown'; threads: 'recorded' | 'unknown' }
  role_name: string
  goal: string
  progress: number | null
  progress_label: string
  choices_made: number
  current_task: string
  status: 'active' | 'completed' | 'abandoned'
  location: string | null
  feedback: string[]
  clues: string[]
  people: JournalPerson[]
  relationships: JournalRelationship[]
  lineage: string[]
  milestones: { label: string; complete: boolean }[]
  recap: { branch_id: string; action: string; effects: string[] }[]
  route_health: {
    signals: RouteHealthSignal[]
    review_recommended: boolean
    window: number | null
    unchanged_state_turns: number | null
    repeated_action_turns: number | null
    source_progress_streak: number | null
    closure_readiness: 'ended' | 'blocked_unknown' | 'blocked_dependency' | 'needs_explanation' | 'checklist_clear' | 'unknown'
    closure_outstanding_count: number | null
    ending_written: boolean
    note: string
  }
}

export interface JourneyGoal {
  id: string
  title: string
  status: 'active' | 'completed' | 'transformed' | 'abandoned'
  reason: string | null
  causeBranchId: string | null
}

export interface JourneyThread {
  id: string
  title: string
  status: 'open' | 'resolved' | 'abandoned' | 'unknown'
  reason: string
  evidence: string
  causeBranchId: string | null
}

export type RouteHealthSignal =
  | 'unchanged_tracked_state'
  | 'repeated_action'
  | 'repeated_body'
  | 'source_progress_stalled'
  | 'source_progress_regressed'

export interface JournalPerson {
  id: string
  name: string
  is_player: boolean
  first_page: number
  last_page: number
  summary: string
  identity: string
  summarized: boolean
  name_evidence: CharacterEvidence | null
  portrait: {
    url: string | null
    source: 'published' | 'preset'
    fallback_key: string
  }
  status: {
    code: 'unknown' | 'alive' | 'dead' | 'departed' | 'missing' | 'injured'
    label: string
    permanence: 'temporary' | 'permanent' | null
    evidence: CharacterEvidence | null
  }
}

export interface CharacterEvidence {
  branch_id: string
  page: number
  quote: string
  source: 'narrative' | 'consequence'
}

export interface JournalRelationship {
  source: string
  target: string
  label: string
  evidence: string
  page: number
  origin?: 'opening'
}

export interface SceneIllustrations {
  available: boolean
  can_generate: boolean
  items: { id?: string; index: number; status: 'idle' | 'queued' | 'generating' | 'ready' | 'failed' | 'cancelled'; reason?: string; url?: string; alt: string; source: 'published' | 'private' }[]
}

export interface OpeningNavigation {
  request: PlayCreateRequest & { request_id: string }
  catalog: PackageCatalog
}
export type EndingType = 'normal' | 'deviation' | 'failure'
export type RouteStructure = { volume: string | null; arc: string | null; beat: string | null; source_progress: string | null }
export type OutlineEvidence = { kind: 'opening' | 'narrative'; branch_id: string; ref: string; quote: string }
export type ClosureItem = {
  id: string
  kind: 'goal' | 'thread' | 'dependency'
  target_id: string
  title: string
  reason: string
  required_disclosure: boolean
  blockers: string[]
  evidence: OutlineEvidence | null
}
export type ClosureCoverage = { goals: 'recorded' | 'unknown'; threads: 'recorded' | 'unknown'; state: 'recorded' | 'unknown' }
export type EarlyEndingReceipt = {
  ending_type: 'early'
  branch_id: string
  closed_at: string
  readiness: string
  coverage: ClosureCoverage
  outstanding: ClosureItem[]
  cleared: ClosureItem[]
  ending_written: false
}
export type NaturalEndingReceipt = {
  branch_id: string
  closed_at: string
  readiness: string
  coverage: ClosureCoverage
  outstanding: ClosureItem[]
  cleared: ClosureItem[]
  ending_type: EndingType
  ending_written: true
  proposal_id: string
  binding_digest: string
}
export type RouteClosure = {
  version: string
  branch_id: string
  root_branch_id: string
  status: string
  structure: RouteStructure
  coverage: ClosureCoverage
  readiness: 'ended' | 'blocked_unknown' | 'blocked_dependency' | 'needs_explanation' | 'checklist_clear'
  outstanding: ClosureItem[]
  cleared: ClosureItem[]
  outstanding_count: number
  cleared_count: number
  ending_written: boolean
  note: string
  lifecycle: {
    phase: 'active' | 'preparing' | 'closing' | 'ended'
    intended_type: EndingType | null
    intent_branch_id: string | null
    ending_type: EndingType | 'early' | null
    receipt: EarlyEndingReceipt | NaturalEndingReceipt | null
    requires_ending_evidence: true
    ending_written: boolean
  } | null
}
export type EndingReviewCheck = { passed: boolean; reason: string; evidence: string | null }
export type EndingReview = {
  decision: 'allow' | 'reject' | 'unknown'
  checks: {
    ending_type_supported: EndingReviewCheck
    threads_accounted_for: EndingReviewCheck
    no_new_unresolved_conflict: EndingReviewCheck
    ending_present: EndingReviewCheck
  }
}
export type EndingMetrics = {
  calls: number
  reported_tokens: number | null
  unreported_calls: number
  tokens: number | null
  elapsed_ms: number
  call_count_source: 'transport_observations' | 'gateway_invocation'
}
export type EndingAudit = {
  model: string
  prompt_version: string
  input: { ending_type: EndingType; outcome_summary: string; ending_quote: string; narrative: string; goals: JsonValue[]; threads: JsonValue[]; conflicts: JsonValue[]; character_outcomes: JsonValue[] }
  raw_response: string | null
  failure: { code: string; message: string } | null
  metrics: EndingMetrics | null
  observations: JsonValue[]
  cancellation?: { at: string; reason: 'user_cancelled'; provider_cancelled: boolean }
  late_result?: { status: EndingProposal['status']; review: EndingReview | null; received_at: string }
}
export type EndingProposal = {
  id: string
  branch_id: string
  request_id: string
  ending_type: EndingType
  outcome_summary: string
  ending_quote: string
  status: 'pending' | 'approved' | 'rejected' | 'failed' | 'stale' | 'committed' | 'cancelled'
  can_cancel: boolean
  created_at: string
  binding_digest: string
  review: EndingReview | null
  audit: EndingAudit | null
}
export type EndingAttempt = { request_id: string; outcome_summary: string; ending_quote: string }
export type EndRouteResponse = { status: 'abandoned' }
export type EndingCommitResponse = { status: 'completed'; receipt: NaturalEndingReceipt }
