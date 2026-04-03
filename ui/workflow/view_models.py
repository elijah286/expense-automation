from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DashboardViewModel:
    total_transactions: int = 0
    matched_transactions: int = 0
    unmatched_transactions: int = 0
    low_confidence_transactions: int = 0
    ready_to_submit: bool = False
    next_action: str = ""


@dataclass(frozen=True)
class DocumentRowViewModel:
    source_file: str
    vendor: str = ""
    transaction_date: str = ""
    amount: str = ""
    confidence: float = 0.0
    parse_status: str = "pending"


@dataclass(frozen=True)
class DocumentsViewModel:
    rows: list[DocumentRowViewModel] = field(default_factory=list)


@dataclass(frozen=True)
class OracleTransactionRowViewModel:
    line_id: str
    merchant: str
    transaction_date: str
    amount: str
    currency: str
    match_status: str
    classification_status: str


@dataclass(frozen=True)
class OracleTransactionsViewModel:
    rows: list[OracleTransactionRowViewModel] = field(default_factory=list)
    scraped_count: int = 0
    matched_count: int = 0
    unmatched_count: int = 0


@dataclass(frozen=True)
class MatchCandidateViewModel:
    receipt_id: str | None
    confidence: float
    confidence_level: str
    reasoning: str


@dataclass(frozen=True)
class MatchingWorkspaceViewModel:
    candidates: list[MatchCandidateViewModel] = field(default_factory=list)
    selected_line_id: str = ""
    selected_receipt_id: str = ""
    selected_confidence: float = 0.0
    selected_reasoning: str = ""


@dataclass(frozen=True)
class ClassificationRowViewModel:
    transaction_id: str
    expense_type: str
    confidence: float
    justification: str
    source: str


@dataclass(frozen=True)
class ClassificationViewModel:
    rows: list[ClassificationRowViewModel] = field(default_factory=list)
    learned_rules_count: int = 0


@dataclass(frozen=True)
class FinalReviewViewModel:
    total_rows: int = 0
    matched_rows: int = 0
    missing_receipts: int = 0
    low_confidence_rows: int = 0
    blockers: list[str] = field(default_factory=list)
    ready: bool = False


@dataclass(frozen=True)
class SubmissionViewModel:
    vpn_required: bool = True
    status_message: str = ""
    timeline_items: list[str] = field(default_factory=list)

