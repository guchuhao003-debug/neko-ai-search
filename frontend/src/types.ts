export type SearchMode = "fast" | "deep";

export interface SearchResult {
    id: number;
    type: "text" | "image" | "video" | "file";
    title: string;
    url: string;
    content: string;
    score?: number | null;
    published_date?: string | null;
    file_type?: string | null;
    thumbnail_url?: string | null;
}

export interface SearchHistoryItem {
    id: string;
    remoteId?: number;
    query: string;
    mode?: SearchMode;
    createdAt: string;
}

export interface AuthUser {
    id: number;
    email: string;
    display_name: string;
    role: string;
    status: string;
    created_at: string;
    is_admin: boolean;
}

export interface AuthStatusResponse {
    user: AuthUser | null;
}

export type UserRole = "user" | "admin";
export type UserStatus = "active" | "disabled";

export interface ApiSearchHistoryItem {
    id: number;
    query: string;
    mode: SearchMode;
    created_at: string;
}

export interface SearchHistoryListResponse {
    items: ApiSearchHistoryItem[];
}

export interface CreditAccount {
    balance: number;
    updated_at: string;
}

export interface CreditLedgerItem {
    id: number;
    change_amount: number;
    balance_after: number;
    reason: string;
    reference_type?: string | null;
    reference_id?: string | null;
    created_at: string;
}

export interface CreditSummaryResponse {
    account: CreditAccount;
    ledger: CreditLedgerItem[];
}

export interface AdminStatsSummary {
    total_users: number;
    active_sessions: number;
    total_history_items: number;
    total_credit_balance: number;
    total_credits_granted: number;
    total_credits_spent: number;
    total_search_debits: number;
    fast_history_items: number;
    deep_history_items: number;
    registered_today: number;
    searches_today: number;
    credits_spent_today: number;
}

export interface AdminRecentUserItem {
    id: number;
    email: string;
    display_name: string;
    balance: number;
    history_count: number;
    created_at: string;
}

export interface AdminRecentSearchItem {
    id: number;
    user_email: string;
    query: string;
    mode: SearchMode;
    created_at: string;
}

export interface AdminCreditReasonItem {
    reason: string;
    ledger_count: number;
    total_change: number;
}

export interface AdminStatsResponse {
    summary: AdminStatsSummary;
    recent_users: AdminRecentUserItem[];
    recent_searches: AdminRecentSearchItem[];
    credit_reasons: AdminCreditReasonItem[];
}

export interface AdminManagedUserItem {
    id: number;
    email: string;
    display_name: string;
    role: UserRole;
    status: UserStatus;
    balance: number;
    history_count: number;
    created_at: string;
    updated_at: string;
}

export interface AdminUserListResponse {
    items: AdminManagedUserItem[];
    total: number;
    limit: number;
    offset: number;
}

export interface AdminCreditAdjustmentResponse {
    user: AdminManagedUserItem;
    account: CreditAccount;
    ledger: CreditLedgerItem;
}

export interface AdminDeleteUserResponse {
    deleted: boolean;
}

export type SearchTraceStepStatus = "running" | "success" | "error";

export interface SearchTraceStep {
    name: string;
    label: string;
    status: SearchTraceStepStatus;
    duration_ms?: number;
    error_message?: string;
}

export interface SseFrame {
    event: string;
    data: unknown;
}
