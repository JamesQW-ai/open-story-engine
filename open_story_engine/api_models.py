"""HTTP contracts for the first, read-only API slice (Python 3.10+)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PackageRef(RequestModel):
    package_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=30)


class ParseRequest(RequestModel):
    package: dict[str, Any]


class ContextRequest(RequestModel):
    package: PackageRef | None = None
    entry_point_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_character_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    parent_branch_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_source(self):
        entry = (self.package, self.entry_point_id, self.source_character_id)
        branch = (self.session_id, self.parent_branch_id)
        if all(value is not None for value in entry) and all(value is None for value in branch):
            return self
        if all(value is not None for value in branch) and all(value is None for value in entry):
            return self
        raise ValueError("请提供包、入口与原著角色，或提供会话与父分支；两种来源不能混用")


class PreviewCapability(BaseModel):
    available: bool = Field(description="预检是否通过；具体入口或分支仍须在预览请求中校验")
    code: str | None
    message: str | None


class PackageSummary(BaseModel):
    package_id: str
    version: str
    title: str
    summary: str
    modular: bool
    module_index_sha256: str | None = None
    beat_count: int
    character_count: int
    context_preview: PreviewCapability


class PackageIssue(BaseModel):
    package_id: str
    version: str
    code: str
    message: str


class PackageList(BaseModel):
    packages: list[PackageSummary]
    issues: list[PackageIssue]


class PublishedScene(BaseModel):
    id: str
    url: str
    alt: str
    source: Literal["published"]


class EntrySummary(BaseModel):
    opening_image: PublishedScene | None = None
    id: str
    title: str
    chapter_title: str
    chapter_id: str | None
    beat_id: str
    node_id: str
    summary: str
    source_character_ids: list[str]
    available_to_new_character: bool = False


class ProfileField(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    id: str
    label: str
    type: Literal["text", "integer"]


class NewCharacterOption(BaseModel):
    enabled: bool
    profile_fields: list[ProfileField]


class BeatSummary(BaseModel):
    id: str
    node_id: str
    summary: str


class CoreObject(BaseModel):
    """Document stable core fields while retaining package-specific JSON data."""

    model_config = ConfigDict(extra="allow", strict=True)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class EntityCard(CoreObject):
    id: str
    name: str | None = None
    description: str | None = None
    menuDescription: str | None = None
    role: str | None = None
    tags: list[str] | None = None
    exits: list[str] | None = None
    portable: bool | None = None


class LedgerSource(CoreObject):
    kind: str
    ref: str
    nodeRef: str | None = None


class LedgerEntry(CoreObject):
    id: str
    sequence: int
    kind: Literal["location", "character", "item", "relationship", "clue", "event"]
    operation: Literal["inherited", "added", "changed"]
    entityId: str
    summary: str
    before: JsonValue
    after: JsonValue
    source: LedgerSource


class BranchLedger(CoreObject):
    schemaVersion: Literal["branch-state-ledger/0.1"]
    entries: list[LedgerEntry]


class StoryState(CoreObject):
    """State fields vary by StoryPackage; absent fields are never synthesized."""

    currentNodeId: str | None = None
    currentLocationId: str | None = None
    playerLocationId: str | None = None
    playerCharacterId: str | None = None
    characterLocationIds: dict[str, str] | None = None
    branchLedger: BranchLedger | None = None


class DirectionView(CoreObject):
    id: str
    title: str
    summary: str | None = None


class BranchView(CoreObject):
    id: str
    sessionId: str
    sequence: int = Field(description="会话内写入序号；剧情父子关系由 parentId 决定")
    parentId: str | None = None
    kind: str
    sourceNodeRef: str
    narrativeText: str
    summary: str
    branchState: StoryState | None = Field(default=None, description="旧分支可能未保存状态；缺失时不得用会话当前状态代替")
    nextDirections: list[DirectionView]
    openThreads: list[str]
    createdAt: str
    canonicalRelation: str | None = None


class BranchSummary(BaseModel):
    id: str
    session_id: str
    parent_id: str | None
    sequence: int
    created_at: str
    action: str | None = None


class BranchPage(BaseModel):
    session_id: str
    branches: list[BranchSummary]
    next_after_sequence: int | None


class SessionRecord(CoreObject):
    id: str
    storyPackageId: str
    storyPackageVersion: str
    storyPackageModuleIndexSha256: str | None
    stateVersion: int
    status: str
    currentState: StoryState


class ChapterView(CoreObject):
    id: str
    title: str


class ContextBeat(CoreObject):
    id: str
    summary: str


class CharacterDetail(CoreObject):
    name: str
    detail: str


class LineRange(CoreObject):
    start: int
    end: int


class NarrativeCue(CoreObject):
    text: str
    evidenceParagraphId: str | None = None
    lineRange: LineRange | None = None


class ContextData(CoreObject):
    world: dict[str, JsonValue]
    currentChapter: ChapterView
    currentBeat: ContextBeat
    narrativeBrief: list[NarrativeCue]
    priorNarrativeBrief: list[NarrativeCue]
    previousSourceLine: int
    actionContract: dict[str, JsonValue]
    modulePaths: list[str]
    previousBeatSummaries: list[str]
    characters: list[EntityCard]
    locations: list[EntityCard]
    items: list[EntityCard]
    characterDetails: list[CharacterDetail]
    characterIdentityEvidence: list[dict[str, JsonValue]]
    sourceDialogueContext: list[dict[str, JsonValue]]
    continuityText: str


class PackageCatalog(BaseModel):
    supporting_characters: list[EntityCard] = Field(default_factory=list)
    package: PackageSummary
    new_character: NewCharacterOption | None = None
    entries: list[EntrySummary]
    beats: list[BeatSummary]
    characters: list[EntityCard]
    locations: list[EntityCard]
    items: list[EntityCard]


class SessionSummary(BaseModel):
    title: str | None = None
    role_name: str | None = None
    recent_progress: str | None = None
    updated_at: str | None = None
    id: str
    package_id: str
    version: str
    status: str
    state_version: int
    created_at: str | None = None


class RenameRequest(RequestModel):
    title: str = Field(min_length=1, max_length=80)


class EndRouteRequest(RequestModel):
    branch_id: str = Field(min_length=1, max_length=200)


class RouteClosureIntentRequest(EndRouteRequest):
    intended_type: Literal['normal', 'deviation', 'failure'] | None


class EndingProposalRequest(EndRouteRequest):
    request_id: str = Field(min_length=1, max_length=200)
    outcome_summary: str = Field(min_length=1, max_length=2000)
    ending_quote: str = Field(min_length=1, max_length=6000)


class EndingProposalView(BaseModel):
    id: str
    branch_id: str
    request_id: str
    binding_digest: str
    ending_type: Literal['normal', 'deviation', 'failure']
    outcome_summary: str
    ending_quote: str
    status: Literal['pending', 'approved', 'rejected', 'failed', 'stale', 'committed', 'cancelled']
    can_cancel: bool = False
    created_at: str
    review: dict[str, Any] | None
    audit: dict[str, Any] | None


class SessionList(BaseModel):
    available: bool
    sessions: list[SessionSummary]


class SessionPreviewCapability(PreviewCapability):
    binding_status: Literal["matched", "legacy_unverified", "changed", "unavailable"]


class SessionView(BaseModel):
    session: SessionRecord
    branches: list[BranchView]
    branches_included: bool
    context_preview: SessionPreviewCapability


class StateView(BaseModel):
    session_id: str
    branch_id: str | None
    state_version: int
    state: StoryState
    ledger: BranchLedger


class RouteMonitorItem(BaseModel):
    id: str
    title: str
    status: str


class RouteClosureView(BaseModel):
    goal_coverage: Literal['recorded', 'unknown']
    thread_coverage: Literal['recorded', 'unknown']
    active_goals: list[RouteMonitorItem]
    unknown_goals: list[RouteMonitorItem]
    open_threads: list[RouteMonitorItem]
    unknown_threads: list[RouteMonitorItem]
    ending_eligibility: Literal['unknown']


class RouteTurnView(BaseModel):
    branch_id: str
    parent_branch_id: str | None
    turn: int
    changed: list[str] | None
    unchanged_state_turns: int
    repeated_action_turns: int
    repeated_body_from: str | None


class RouteMonitorView(BaseModel):
    version: str
    branch_id: str
    root_branch_id: str
    status: str
    turns: int
    recent_turns: list[RouteTurnView]
    coverage: Literal['recorded', 'unknown']
    unknown_state_branches: list[str]
    signals: list[Literal['unchanged_tracked_state', 'repeated_action', 'repeated_body', 'source_progress_stalled', 'source_progress_regressed']]
    review_recommended: bool
    window: int
    unchanged_state_turns: int
    repeated_action_turns: int
    source_progress_streak: int
    closure: RouteClosureView


class OutlineEvidence(BaseModel):
    kind: Literal['opening', 'narrative']
    branch_id: str
    ref: str
    quote: str


class OutlineCompletion(BaseModel):
    ledger: Literal['goalLedger', 'threadLedger']
    target_id: str
    target_statuses: list[str]
    met: bool | None
    requires_narrative_evidence: Literal[True]


class OutlineObligation(BaseModel):
    id: str
    title: str
    status: str
    evidence: OutlineEvidence | None
    completion: OutlineCompletion


class OutlineStep(BaseModel):
    id: str
    kind: Literal['verify_status', 'review_dependency', 'pursue_goal', 'address_thread']
    target_id: str
    title: str
    blockers: list[str]
    evidence: OutlineEvidence | None
    completion: OutlineCompletion


class OutlineConflict(BaseModel):
    id: str
    goal_id: str
    character_id: str
    status: Literal['dead', 'departed', 'missing']
    evidence: OutlineEvidence


class OutlineItemConflict(BaseModel):
    id: str
    target_kind: Literal['goal', 'thread']
    target_id: str
    item_id: str
    status: Literal['destroyed', 'unknown']
    evidence: OutlineEvidence | None


class RouteOutline(BaseModel):
    version: str
    branch_id: str
    root_branch_id: str
    binding_digest: str
    structure: dict[str, str | None]
    goals: list[OutlineObligation]
    threads: list[OutlineObligation]
    conflicts: list[OutlineConflict | OutlineItemConflict]
    steps: list[OutlineStep]


class RouteOutlineView(BaseModel):
    outline: RouteOutline
    storage: Literal['saved', 'reconstructed']
    status: str
    actionable: bool


class ClosureItem(BaseModel):
    id: str
    kind: Literal['goal', 'thread', 'dependency']
    target_id: str
    title: str
    reason: str
    required_disclosure: bool
    blockers: list[str]
    evidence: OutlineEvidence | None


class ClosureCoverage(BaseModel):
    goals: Literal['recorded', 'unknown']
    threads: Literal['recorded', 'unknown']
    state: Literal['recorded', 'unknown']


class EarlyEndingReceipt(BaseModel):
    ending_type: Literal['early']
    branch_id: str
    closed_at: str
    readiness: str
    coverage: ClosureCoverage
    outstanding: list[ClosureItem]
    cleared: list[ClosureItem]
    ending_written: Literal[False]


class NaturalEndingReceipt(EarlyEndingReceipt):
    ending_type: Literal['normal', 'deviation', 'failure']
    ending_written: Literal[True]
    proposal_id: str
    binding_digest: str


class RouteLifecycle(BaseModel):
    phase: Literal['active', 'preparing', 'closing', 'ended']
    intended_type: Literal['normal', 'deviation', 'failure'] | None
    intent_branch_id: str | None
    ending_type: Literal['early', 'normal', 'deviation', 'failure'] | None
    receipt: EarlyEndingReceipt | NaturalEndingReceipt | None
    requires_ending_evidence: Literal[True]
    ending_written: bool


class RouteClosurePreparation(BaseModel):
    version: str
    branch_id: str
    root_branch_id: str
    status: str
    structure: dict[str, str | None]
    coverage: ClosureCoverage
    outstanding: list[ClosureItem]
    cleared: list[ClosureItem]
    outstanding_count: int
    cleared_count: int
    readiness: Literal['ended', 'blocked_unknown', 'blocked_dependency', 'needs_explanation', 'checklist_clear']
    ending_written: bool
    note: str
    lifecycle: RouteLifecycle | None = None


class ContextView(BaseModel):
    mode: Literal["entry_preview", "branch_preview"]
    package: PackageSummary
    session_id: str | None
    parent_branch_id: str | None
    context_sha256: str
    state: StoryState
    context: ContextData


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    phase: Literal["read_only", "play"] = "read_only"
    generation_available: bool = False
    state_updates_available: bool = False


class PlayCreateRequest(RequestModel):
    package: PackageRef
    entry_point_id: str = Field(min_length=1, max_length=200)
    source_character_id: str | None = Field(default=None, min_length=1, max_length=200)
    new_character: dict[str, JsonValue] | None = None
    identity_opening: bool = False
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


class PlayCreateResponse(BaseModel):
    session: SessionRecord
    branch: BranchView


class PrepareChoicesRequest(RequestModel):
    parent_branch_id: str = Field(min_length=1, max_length=200)
    subscriber_id: str = Field(min_length=1, max_length=100)
    history_id: str | None = Field(default=None, min_length=1, max_length=200)


class ReadingReceiptRequest(RequestModel):
    branch_id: str = Field(min_length=1, max_length=200)


class PlayContinueRequest(RequestModel):
    history_id: str | None = Field(default=None, min_length=1, max_length=200)
    draft_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    choice_id: str | None = Field(default=None, min_length=1, max_length=200)
    subscriber_id: str | None = Field(default=None, min_length=1, max_length=100)
    parent_branch_id: str = Field(min_length=1, max_length=200)
    direction_id: str | None = Field(default=None, min_length=1, max_length=200)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


class PlayContinueResponse(BaseModel):
    status: Literal["written", "rejected"]
    request_id: str
    deduplicated: bool
    branch: BranchView | None = None
    kind: str | None = None
    reason: str | None = None


class SourceChapterView(BaseModel):
    session_id: str
    branch_id: str
    chapter_id: str
    title: str
    text: str
    source_progress: str


class CharacterProfileRequest(RequestModel):
    branch_id: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=1, max_length=200)
