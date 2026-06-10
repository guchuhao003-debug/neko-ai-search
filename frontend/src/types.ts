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
    created_at: string;
}

export interface AuthStatusResponse {
    user: AuthUser | null;
}

export interface ApiSearchHistoryItem {
    id: number;
    query: string;
    mode: SearchMode;
    created_at: string;
}

export interface SearchHistoryListResponse {
    items: ApiSearchHistoryItem[];
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
