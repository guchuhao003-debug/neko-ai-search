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
    query: string;
    createdAt: string;
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
