export interface SearchResult {
    id: number;
    title: string;
    url: string;
    content: string;
    score?: number | null;
    published_date?: string | null;
}

export interface SearchHistoryItem {
    id: string;
    query: string;
    createdAt: string;
}

export interface SseFrame {
    event: string;
    data: unknown;
}
