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

export interface EntrySummary {
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

export interface PlayContinueRequest {
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
  role_name: string
  goal: string
  progress: number
  progress_label: string
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
}

export interface JournalPerson {
  id: string
  name: string
  is_player: boolean
  first_page: number
  last_page: number
  summary: string
  identity: string
  summarized: boolean
}

export interface JournalRelationship {
  source: string
  target: string
  label: string
  evidence: string
  page: number
}

export interface SceneIllustrations {
  available: boolean
  items: { index: number; status: 'idle' | 'pending' | 'ready' | 'failed'; url?: string; alt: string }[]
}
