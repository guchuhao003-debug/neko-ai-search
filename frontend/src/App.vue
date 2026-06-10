<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { marked } from "marked";
import { markedHighlight } from "marked-highlight";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import {
    ArrowUp,
    ArrowUpRight,
    Bolt,
    CheckCircle2,
    CircleAlert,
    Clock3,
    FileText,
    Globe2,
    Image,
    Layers3,
    LogIn,
    LogOut,
    Loader2,
    Moon,
    PlayCircle,
    RotateCcw,
    Search,
    ShieldCheck,
    Sparkles,
    Sun,
    Trash2,
    UserRound,
    X,
} from "@lucide/vue";
import type {
    AuthStatusResponse,
    AuthUser,
    SearchHistoryListResponse,
    SearchHistoryItem,
    SearchMode,
    SearchResult,
    SearchTraceStep,
    SseFrame,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const THEME_KEY = "neko-ai-search-theme";
const GITHUB_REPO_URL = "https://github.com/guchuhao003-debug/neko-ai-search.git";
const logoUrl = new URL("./assets/neko-search-logo.svg", import.meta.url).href;
const officialQrUrl = new URL("./assets/neko-official-qr.jpg", import.meta.url).href;

hljs.registerLanguage("bash", bash);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("markdown", markdown);
hljs.registerLanguage("python", python);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("xml", xml);

marked.use(
    markedHighlight({
        langPrefix: "hljs language-",
        highlight(code, lang) {
            const language = hljs.getLanguage(lang) ? lang : "plaintext";
            return hljs.highlight(code, { language }).value;
        },
    }),
);

const query = ref("");
const activeQuery = ref("");
const searchMode = ref<SearchMode>("fast");
const answer = ref("");
const errorMessage = ref("");
const isSearching = ref(false);
const searchResults = ref<SearchResult[]>([]);
const relatedQuestions = ref<string[]>([]);
const searchId = ref("");
const traceSteps = ref<SearchTraceStep[]>([]);
const history = ref<SearchHistoryItem[]>([]);
const historyLoading = ref(false);
const currentUser = ref<AuthUser | null>(null);
const isAuthModalOpen = ref(false);
const authMode = ref<"login" | "register">("login");
const authEmail = ref("");
const authPassword = ref("");
const authDisplayName = ref("");
const authError = ref("");
const authBusy = ref(false);
const isDarkTheme = ref(loadThemePreference());
const currentPage = ref<"home" | "guide">(loadInitialPage());
const failedPreviewIds = ref<Set<number>>(new Set());
const directPreviewIds = ref<Set<number>>(new Set());
const showAllImages = ref(false);
const hotSearches = [
    "DeepSeek V4 有哪些能力？",
    "国产 AI 大模型有哪些？",
    "AI 如何改变未来工作方式？",
    "量子计算的最新进展",
];

const hasSearched = computed(() => activeQuery.value.length > 0);
const isAuthenticated = computed(() => currentUser.value !== null);
const historyScopeLabel = computed(() => (isAuthenticated.value ? "云端私有历史" : "用户级后端历史"));
const historyHint = computed(() => {
    return isAuthenticated.value
        ? "仅当前账号可见"
        : "登录后自动保存到你的账号";
});
const renderedAnswer = computed(() => renderAnswerMarkdown(answer.value, searchResults.value));
const referencedSources = computed(() => pickReferencedSources(answer.value, searchResults.value));
const themeToggleLabel = computed(() => (isDarkTheme.value ? "切换为亮色主题" : "切换为暗色主题"));
const standaloneImageResults = computed(() => searchResults.value.filter(isStandaloneImageResult));
const listedSearchResults = computed(() => {
    return searchResults.value.filter((result) => result.type !== "image");
});
const visibleImageResults = computed(() => {
    return showAllImages.value
        ? standaloneImageResults.value
        : standaloneImageResults.value.slice(0, 4);
});


/**
 * Normalize backend history items for the shared sidebar renderer.
 */
function mapRemoteHistory(item: SearchHistoryListResponse["items"][number]): SearchHistoryItem {
    return {
        id: String(item.id),
        remoteId: item.id,
        query: item.query,
        mode: item.mode,
        createdAt: item.created_at,
    };
}

class ApiRequestError extends Error {
    status: number;

    /**
     * Create an API error that keeps the HTTP status code available.
     */
    constructor(status: number, message: string) {
        super(message);
        this.status = status;
    }
}

/**
 * Extract a readable error message from backend JSON error responses.
 */
async function readApiError(response: Response): Promise<string> {
    try {
        const payload = await response.json();
        if (typeof payload.detail === "string") {
            return payload.detail;
        }
        if (payload.detail && typeof payload.detail.message === "string") {
            return payload.detail.message;
        }
        if (typeof payload.message === "string") {
            return payload.message;
        }
    } catch {
        return `请求失败：HTTP ${response.status}`;
    }

    return `请求失败：HTTP ${response.status}`;
}

/**
 * Send an API request with credentials so Session Cookie auth works cross-origin.
 */
async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
    const headers = new Headers(options.headers);
    if (options.body && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
    }

    const response = await fetch(`${API_BASE_URL}${path}`, {
        ...options,
        headers,
        credentials: "include",
    });
    if (!response.ok) {
        throw new ApiRequestError(response.status, await readApiError(response));
    }

    return response.json() as Promise<T>;
}

/**
 * Load account state from the backend session cookie on page entry.
 */
async function refreshAuthStatus(): Promise<void> {
    try {
        const status = await apiRequest<AuthStatusResponse>("/api/auth/me");
        currentUser.value = status.user;
        if (status.user) {
            await loadRemoteHistory();
            return;
        }
    } catch {
        currentUser.value = null;
    }

    history.value = [];
}

/**
 * Load private search history for the current authenticated user.
 */
async function loadRemoteHistory(): Promise<void> {
    if (!currentUser.value) {
        return;
    }

    historyLoading.value = true;
    try {
        const response = await apiRequest<SearchHistoryListResponse>("/api/history");
        history.value = response.items.map(mapRemoteHistory);
    } catch (error) {
        if (error instanceof ApiRequestError && error.status === 401) {
            currentUser.value = null;
            history.value = [];
        }
    } finally {
        historyLoading.value = false;
    }
}

/**
 * Open the login or register modal with a clean error state.
 */
function openAuthModal(mode: "login" | "register" = "login"): void {
    authMode.value = mode;
    authError.value = "";
    isAuthModalOpen.value = true;
}

/**
 * Close the auth modal unless a request is actively running.
 */
function closeAuthModal(): void {
    if (authBusy.value) {
        return;
    }

    isAuthModalOpen.value = false;
    authError.value = "";
}

/**
 * Switch between login and registration inside the modal.
 */
function switchAuthMode(mode: "login" | "register"): void {
    authMode.value = mode;
    authError.value = "";
}

/**
 * Submit login or registration and refresh user-scoped history afterwards.
 */
async function submitAuthForm(): Promise<void> {
    if (authBusy.value) {
        return;
    }

    authBusy.value = true;
    authError.value = "";
    const endpoint = authMode.value === "login" ? "/api/auth/login" : "/api/auth/register";
    const displayName = authDisplayName.value.trim() || authEmail.value.split("@")[0];
    const payload = authMode.value === "login"
        ? { email: authEmail.value.trim(), password: authPassword.value }
        : {
            email: authEmail.value.trim(),
            password: authPassword.value,
            display_name: displayName,
        };

    try {
        const response = await apiRequest<AuthStatusResponse>(endpoint, {
            method: "POST",
            body: JSON.stringify(payload),
        });
        currentUser.value = response.user;
        isAuthModalOpen.value = false;
        authPassword.value = "";
        authError.value = "";
        if (response.user) {
            await loadRemoteHistory();
        }
    } catch (error) {
        authError.value = error instanceof Error ? error.message : "认证失败，请稍后重试";
    } finally {
        authBusy.value = false;
    }
}

/**
 * Logout the current user and clear user-scoped history from the sidebar.
 */
async function logout(): Promise<void> {
    try {
        await apiRequest<AuthStatusResponse>("/api/auth/logout", { method: "POST" });
    } finally {
        currentUser.value = null;
        history.value = [];
    }
}

/**
 * Load the persisted theme preference, falling back to the system preference.
 */
function loadThemePreference(): boolean {
    try {
        const stored = localStorage.getItem(THEME_KEY);
        if (stored === "dark") {
            return true;
        }
        if (stored === "light") {
            return false;
        }
    } catch {
        return false;
    }

    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

/**
 * Read the initial lightweight page state from the current hash.
 */
function loadInitialPage(): "home" | "guide" {
    return window.location.hash === "#guide" ? "guide" : "home";
}

/**
 * Keep the page state aligned with browser back and direct hash navigation.
 */
function syncPageFromHash(): void {
    currentPage.value = loadInitialPage();
}

/**
 * Navigate to the standalone platform guide page.
 */
function showGuidePage(): void {
    activeQuery.value = "";
    answer.value = "";
    errorMessage.value = "";
    currentPage.value = "guide";
    window.history.pushState(null, "", "#guide");
    window.scrollTo({ top: 0, behavior: "smooth" });
}

/**
 * Return to the home search page and clear the visible result state.
 */
function showHomePage(): void {
    activeQuery.value = "";
    answer.value = "";
    errorMessage.value = "";
    currentPage.value = "home";
    window.history.pushState(null, "", window.location.pathname + window.location.search);
    window.scrollTo({ top: 0, behavior: "smooth" });
}

/**
 * Toggle the visual theme and persist the user's preference locally.
 */
function toggleTheme(): void {
    isDarkTheme.value = !isDarkTheme.value;
    localStorage.setItem(THEME_KEY, isDarkTheme.value ? "dark" : "light");
}

onMounted(() => {
    window.addEventListener("hashchange", syncPageFromHash);
    void refreshAuthStatus();
});

onBeforeUnmount(() => {
    window.removeEventListener("hashchange", syncPageFromHash);
});

/**
 * Add a query to the visible history list without persisting it remotely.
 */
function upsertVisibleHistory(value: string): void {
    const item = {
        id: crypto.randomUUID(),
        query: value,
        mode: searchMode.value,
        createdAt: new Date().toISOString(),
    };
    history.value = [item, ...history.value.filter((entry) => entry.query !== value)].slice(0, 8);
}

/**
 * Remove one backend history row owned by the current account.
 */
async function removeHistoryItem(item: SearchHistoryItem): Promise<void> {
    if (currentUser.value && item.remoteId !== undefined) {
        const response = await apiRequest<SearchHistoryListResponse>(
            `/api/history/${item.remoteId}`,
            { method: "DELETE" },
        );
        history.value = response.items.map(mapRemoteHistory);
        return;
    }
}

/**
 * Clear backend history for the current authenticated account.
 */
async function clearCurrentHistory(): Promise<void> {
    if (currentUser.value) {
        const response = await apiRequest<SearchHistoryListResponse>(
            "/api/history",
            { method: "DELETE" },
        );
        history.value = response.items.map(mapRemoteHistory);
        return;
    }

    history.value = [];
}
/**
 * Submit a search request and consume backend SSE frames.
 */
async function submitSearch(nextQuery = query.value): Promise<void> {
    const trimmed = nextQuery.trim();
    if (!trimmed || isSearching.value) {
        return;
    }

    query.value = trimmed;
    activeQuery.value = trimmed;
    currentPage.value = "home";
    answer.value = "";
    errorMessage.value = "";
    searchResults.value = [];
    relatedQuestions.value = [];
    searchId.value = "";
    traceSteps.value = [];
    failedPreviewIds.value = new Set();
    directPreviewIds.value = new Set();
    showAllImages.value = false;
    isSearching.value = true;
    if (currentUser.value) {
        upsertVisibleHistory(trimmed);
    }

    try {
        const response = await fetch(`${API_BASE_URL}/api/search/stream`, {
            method: "POST",
            credentials: "include",
            headers: {
                "Content-Type": "application/json",
                Accept: "text/event-stream",
            },
            body: JSON.stringify({ query: trimmed, mode: searchMode.value }),
        });

        if (!response.ok || !response.body) {
            throw new Error(`Search request failed: HTTP ${response.status}`);
        }

        await readSseStream(response.body);
    } catch (error) {
        errorMessage.value = error instanceof Error
            ? error.message
            : "Search failed. Please try again.";
    } finally {
        isSearching.value = false;
        if (currentUser.value) {
            void loadRemoteHistory();
        }
    }
}
/**
 * Incrementally decode the ReadableStream and split complete SSE frames.
 */
async function readSseStream(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) {
            break;
        }

        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames.map(parseSseFrame)) {
            if (frame) {
                handleSseFrame(frame);
            }
        }
    }

    if (buffer.trim()) {
        const frame = parseSseFrame(buffer);
        if (frame) {
            handleSseFrame(frame);
        }
    }
}

/**
 * Parse one SSE frame into a typed event payload.
 */
function parseSseFrame(raw: string): SseFrame | null {
    const eventLine = raw.split("\n").find((line) => line.startsWith("event:"));
    const dataLine = raw.split("\n").find((line) => line.startsWith("data:"));
    if (!eventLine || !dataLine) {
        return null;
    }

    const event = eventLine.replace("event:", "").trim();
    const text = dataLine.replace("data:", "").trim();
    try {
        return { event, data: JSON.parse(text) };
    } catch {
        return { event, data: text };
    }
}

/**
 * Apply backend SSE events to the current UI state.
 */
function handleSseFrame(frame: SseFrame): void {
    const data = frame.data as Record<string, unknown>;

    if (frame.event === "sources" && Array.isArray(data.results)) {
        searchResults.value = data.results as SearchResult[];
    }

    if (frame.event === "trace_start" && typeof data.search_id === "string") {
        searchId.value = data.search_id;
    }

    if (frame.event === "step_start") {
        upsertTraceStep(data, "running");
    }

    if (frame.event === "step_done") {
        upsertTraceStep(data, "success");
    }

    if (frame.event === "step_error") {
        upsertTraceStep(data, "error");
    }

    if (frame.event === "token" && typeof data.text === "string") {
        answer.value += data.text;
    }

    if (frame.event === "answer_done" && typeof data.answer === "string") {
        answer.value = data.answer;
    }

    if (frame.event === "related" && Array.isArray(data.questions)) {
        relatedQuestions.value = data.questions as string[];
    }

    if (frame.event === "error" && typeof data.message === "string") {
        const trace = typeof data.search_id === "string" ? `（搜索 ID：${data.search_id}）` : "";
        errorMessage.value = `${data.message}${trace}`;
    }
}

/**
 * Insert or update one trace step in the current search process.
 */
function upsertTraceStep(
    data: Record<string, unknown>,
    status: SearchTraceStep["status"],
): void {
    const name = typeof data.step === "string" ? data.step : "unknown";
    const duration = typeof data.duration_ms === "number" ? data.duration_ms : undefined;
    const error = typeof data.error_message === "string" ? data.error_message : undefined;
    const nextStep: SearchTraceStep = {
        name,
        label: traceStepLabel(name),
        status,
        duration_ms: duration,
        error_message: error,
    };
    const index = traceSteps.value.findIndex((step) => step.name === name);

    if (index >= 0) {
        traceSteps.value[index] = { ...traceSteps.value[index], ...nextStep };
        return;
    }

    traceSteps.value.push(nextStep);
}

/**
 * Convert backend step names into user-facing labels.
 */
function traceStepLabel(name: string): string {
    const labels: Record<string, string> = {
        rate_limit: "频率限制检查",
        security_check: "安全策略检查",
        stream_concurrency: "并发连接检查",
        cache_lookup: "缓存检查",
        external_quota: "外部调用配额检查",
        source_search: "来源搜索与排序",
        ai_answer_stream: "AI 回答生成",
        related_questions: "相关问题生成",
        cache_write: "缓存写入",
    };
    return labels[name] ?? name;
}

/**
 * Format step duration for compact trace rows.
 */
function formatDuration(duration?: number): string {
    if (duration === undefined) {
        return "";
    }

    return duration >= 1000 ? `${(duration / 1000).toFixed(1)}s` : `${duration}ms`;
}

/**
 * Extract cited source IDs from Markdown answer text and map them to results.
 */
function pickReferencedSources(text: string, results: SearchResult[]): SearchResult[] {
    const ids = new Set<number>();
    for (const match of text.matchAll(/\[(\d+)]/g)) {
        ids.add(Number(match[1]));
    }
    return results.filter((result) => ids.has(result.id));
}

/**
 * Render answer Markdown and turn source markers like [1] into source links.
 */
function renderAnswerMarkdown(text: string, results: SearchResult[]): string {
    const sourceUrls = new Map(results.map((result) => [result.id, result.url]));
    const markdown = text.replace(/\[(\d+)]/g, (raw, idText: string) => {
        const url = sourceUrls.get(Number(idText));
        return url ? `[[${idText}]](${url})` : raw;
    });

    return String(marked.parse(markdown || ""));
}

/**
 * Navigate to an original source in the current browser tab.
 */
function openExternal(url: string): void {
    window.location.href = url;
}

/**
 * Convert a URL into a compact hostname label for dense result rows.
 */
function hostLabel(url: string): string {
    try {
        return new URL(url).hostname.replace(/^www\./, "");
    } catch {
        return url;
    }
}

/**
 * Convert backend result type into a concise UI label.
 */
function resultTypeLabel(result: SearchResult): string {
    if (result.type === "file") {
        return result.file_type ?? "文件";
    }

    const labels: Record<SearchResult["type"], string> = {
        text: "网页",
        image: "图片",
        video: "视频",
        file: "文件",
    };
    return labels[result.type];
}

/**
 * Check whether a result is a standalone image resource instead of a web article.
 */
function isStandaloneImageResult(result: SearchResult): boolean {
    return result.type === "image";
}

/**
 * Read the compact file type label for file result cards.
 */
function fileTypeLabel(result: SearchResult): string {
    if (result.file_type) {
        return result.file_type;
    }

    try {
        const pathname = new URL(result.url).pathname;
        const extension = pathname.split(".").pop()?.toUpperCase();
        return extension && extension.length <= 6 ? extension : "FILE";
    } catch {
        return "FILE";
    }
}

/**
 * Create a common video thumbnail when the provider does not supply one.
 */
function videoThumbnailUrl(url: string): string {
    try {
        const parsed = new URL(url);
        if (parsed.hostname.includes("youtube.com")) {
            const videoId = parsed.searchParams.get("v");
            return videoId ? `https://img.youtube.com/vi/${videoId}/hqdefault.jpg` : "";
        }

        if (parsed.hostname.includes("youtu.be")) {
            const videoId = parsed.pathname.replace("/", "");
            return videoId ? `https://img.youtube.com/vi/${videoId}/hqdefault.jpg` : "";
        }
    } catch {
        return "";
    }

    return "";
}

/**
 * Read a remote media URL through the backend proxy unless it is already local data.
 */
function proxiedMediaUrl(url: string): string {
    if (!url || url.startsWith("data:") || url.startsWith("blob:")) {
        return url;
    }

    return `${API_BASE_URL}/api/media-proxy?url=${encodeURIComponent(url)}`;
}

/**
 * Read the final image source for a result preview.
 */
function mediaPreviewSrc(result: SearchResult): string {
    const url = previewUrl(result);
    if (!url) {
        return "";
    }

    return directPreviewIds.value.has(result.id) ? url : proxiedMediaUrl(url);
}

/**
 * Retry a failed proxied preview directly before falling back to a placeholder.
 */
function handlePreviewError(resultId: number): void {
    if (!directPreviewIds.value.has(resultId)) {
        directPreviewIds.value = new Set([...directPreviewIds.value, resultId]);
        return;
    }

    failedPreviewIds.value = new Set([...failedPreviewIds.value, resultId]);
}

/**
 * Read the best visual preview URL for media result cards, with failure fallback.
 */
function previewUrl(result: SearchResult): string {
    if (failedPreviewIds.value.has(result.id) || result.type === "file") {
        return "";
    }

    if (result.thumbnail_url) {
        return result.thumbnail_url;
    }

    if (result.type === "image") {
        return result.url;
    }

    if (result.type === "video") {
        return videoThumbnailUrl(result.url);
    }

    return "";
}
</script>

<template>
    <main
        :class="[
            'app-shell',
            {
                'is-results': hasSearched,
                'is-guide': currentPage === 'guide' && !hasSearched,
                'is-dark': isDarkTheme,
            },
        ]"
    >
        <section
            v-if="currentPage === 'home' && !hasSearched"
            class="home-view"
            aria-labelledby="home-title"
        >
            <!-- 首页导航用于承载品牌、入口和主题切换。 -->
            <header class="home-nav" aria-label="首页导航">
                <button
                    class="home-brand"
                    type="button"
                    aria-label="Neko AI Search 首页"
                    @click="showHomePage"
                >
                    <img :src="logoUrl" alt="" />
                    <span class="brand-name">Neko AI Search</span>
                </button>
                <div class="home-actions">
                    <button
                        class="icon-button"
                        type="button"
                        :aria-label="themeToggleLabel"
                        :title="themeToggleLabel"
                        @click="toggleTheme"
                    >
                        <Sun v-if="isDarkTheme" :size="18" />
                        <Moon v-else :size="18" />
                    </button>
                    <button
                        v-if="currentUser"
                        class="auth-action is-signed"
                        type="button"
                        title="刷新云端历史"
                        @click="loadRemoteHistory"
                    >
                        <UserRound :size="16" />
                        <span>{{ currentUser.display_name }}</span>
                    </button>
                    <button
                        v-if="currentUser"
                        class="icon-button"
                        type="button"
                        aria-label="退出登录"
                        title="退出登录"
                        @click="logout"
                    >
                        <LogOut :size="17" />
                    </button>
                    <button
                        v-else
                        class="auth-action"
                        type="button"
                        @click="openAuthModal('login')"
                    >
                        <LogIn :size="16" />
                        <span>登录</span>
                    </button>
                    <button class="outline-action" type="button" @click="showGuidePage">
                        平台指南
                    </button>
                    <a
                        class="solid-action github-action"
                        :href="GITHUB_REPO_URL"
                        target="_blank"
                        rel="noreferrer"
                        aria-label="打开 Neko AI Search GitHub 仓库"
                    >
                        <svg
                            class="github-mark"
                            viewBox="0 0 24 24"
                            aria-hidden="true"
                            focusable="false"
                        >
                            <path
                                fill="currentColor"
                                d="
                                    M12 2C6.48 2 2 6.58 2 12.22c0 4.52 2.87 8.35 6.84 9.7
                                    .5.1.68-.22.68-.5v-1.75c-2.78.62-3.37-1.37-3.37-1.37
                                    -.45-1.18-1.11-1.5-1.11-1.5-.91-.63.07-.62.07-.62
                                    1 .07 1.53 1.06 1.53 1.06.9 1.56 2.35 1.11 2.92.85
                                    .09-.66.35-1.11.63-1.36-2.22-.26-4.56-1.14-4.56-5.05
                                    0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71
                                    0 0 .84-.27 2.75 1.05A9.36 9.36 0 0 1 12 6.93
                                    c.85 0 1.7.12 2.5.34 1.9-1.32 2.74-1.05 2.74-1.05
                                    .55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75
                                    0 3.92-2.34 4.78-4.57 5.03.36.32.68.94.68 1.9v2.81
                                    c0 .28.18.6.69.5A10.11 10.11 0 0 0 22 12.22
                                    C22 6.58 17.52 2 12 2Z
                                "
                            />
                        </svg>
                        <span>GitHub</span>
                        <ArrowUpRight :size="15" aria-hidden="true" />
                    </a>
                </div>
            </header>

            <!-- 首屏主体保留左侧 AI 搜索和右侧指南动画。 -->
            <div class="hero-stage">
                <section class="search-lab" aria-label="Neko AI Search AI 搜索引擎">
                    <h1 id="home-title">
                        <span class="hero-title-accent">Neko AI 驱动引擎</span>，搜得更快，判断更稳
                    </h1>
                    <p class="hero-copy">
                        基于 AI 驱动检索全网信息，生成权威性带引用的智能回答和搜索结果，并保留可追溯来源。
                    </p>

                    <form class="hero-search" @submit.prevent="submitSearch()">
                        <label class="sr-only" for="home-search">输入你的问题</label>
                        <div class="ask-card">
                            <div class="input-line">
                                <Search class="search-box-icon" :size="21" aria-hidden="true" />
                                <input
                                    id="home-search"
                                    v-model="query"
                                    autocomplete="off"
                                    placeholder="问一个需要搜索和验证的问题"
                                />
                            </div>
                            <div class="ask-footer">
                                <div class="mode-toggle" aria-label="搜索模式">
                                    <button
                                        type="button"
                                        :class="{ 'is-active': searchMode === 'fast' }"
                                        @click="searchMode = 'fast'"
                                    >
                                        快速
                                    </button>
                                    <button
                                        type="button"
                                        :class="{ 'is-active': searchMode === 'deep' }"
                                        @click="searchMode = 'deep'"
                                    >
                                        深度
                                    </button>
                                </div>
                                <button
                                    class="submit-button"
                                    type="submit"
                                    aria-label="搜索"
                                    title="搜索"
                                    :disabled="isSearching || query.trim().length < 2"
                                >
                                    <Loader2 v-if="isSearching" class="spin" :size="18" />
                                    <ArrowUp v-else :size="20" aria-hidden="true" />
                                    <span class="sr-only">搜索</span>
                                </button>
                            </div>
                        </div>
                    </form>

                    <div id="home-prompts" class="prompt-strip" aria-label="热门搜索">
                        <div class="prompt-strip-header">
                            <span class="prompt-signal" aria-hidden="true" />
                            <span>推荐搜索</span>
                        </div>
                        <button
                            v-for="(item, index) in hotSearches"
                            :key="item"
                            type="button"
                            @click="submitSearch(item)"
                        >
                            <span class="prompt-index">
                                {{ String(index + 1).padStart(2, "0") }}
                            </span>
                            <span class="prompt-title">{{ item }}</span>
                            <span class="prompt-action">
                                <ArrowUpRight :size="15" aria-hidden="true" />
                            </span>
                        </button>
                    </div>

                    <div class="trust-row" aria-label="平台能力">
                        <span class="trust-item">
                            <span class="trust-index">01</span>
                            <span class="trust-icon"><Bolt :size="15" /></span>
                            <strong>SSE 流式生成</strong>
                            <small>答案实时推进</small>
                        </span>
                        <span class="trust-item">
                            <span class="trust-index">02</span>
                            <span class="trust-icon"><Layers3 :size="15" /></span>
                            <strong>多源结果排序</strong>
                            <small>优先可信来源</small>
                        </span>
                        <span class="trust-item">
                            <span class="trust-index">03</span>
                            <span class="trust-icon"><ShieldCheck :size="15" /></span>
                            <strong>安全过滤</strong>
                            <small>降低噪声干扰</small>
                        </span>
                    </div>
                </section>

                <section
                    id="guide-demo"
                    class="guide-video"
                    aria-label="平台指南展示动画视频"
                >
                    <div class="guide-frame" role="img" aria-label="Neko AI Search 指南演示">
                        <div class="guide-grid" aria-hidden="true" />

                        <article class="demo-card terminal-card">
                            <div class="terminal-titlebar">
                                <div class="terminal-dots" aria-hidden="true">
                                    <span />
                                    <span />
                                    <span />
                                </div>
                                <span>Neko AI Search</span>
                            </div>

                            <div class="terminal-body">
                                <div class="demo-card-header">
                                    <span class="demo-mark">
                                        <img :src="logoUrl" alt="" />
                                    </span>
                                    <strong>Neko AI Search</strong>
                                </div>

                                <div class="terminal-line is-command">
                                    <span class="terminal-prompt">$</span>
                                    <span>neko search --verify “AI 搜索结果可信度”</span>
                                </div>

                                <div class="terminal-line">
                                    <span class="terminal-prompt">›</span>
                                    <span>正在连接多源搜索管线...</span>
                                </div>

                                <ol class="guide-steps">
                                    <li class="is-complete">
                                        <CheckCircle2 :size="16" />
                                        <span>识别问题意图</span>
                                    </li>
                                    <li class="is-active">
                                        <Loader2 class="spin" :size="16" />
                                        <span>检索并排序来源</span>
                                    </li>
                                    <li>
                                        <span class="step-dot" />
                                        <span>生成带引用答案</span>
                                    </li>
                                </ol>

                                <div class="answer-preview">
                                    <span />
                                    <span />
                                    <span />
                                </div>
                            </div>
                        </article>

                        <div class="video-timeline" aria-hidden="true">
                            <span />
                        </div>
                    </div>
                </section>
            </div>
        </section>

        <section
            v-else-if="currentPage === 'guide' && !hasSearched"
            class="guide-page"
            aria-labelledby="guide-title"
        >
            <header class="home-nav guide-nav" aria-label="平台指南导航">
                <button
                    class="home-brand"
                    type="button"
                    aria-label="返回 Neko AI Search 首页"
                    @click="showHomePage"
                >
                    <img :src="logoUrl" alt="" />
                    <span class="brand-name">Neko AI Search</span>
                </button>
                <div class="home-actions">
                    <button
                        class="icon-button"
                        type="button"
                        :aria-label="themeToggleLabel"
                        :title="themeToggleLabel"
                        @click="toggleTheme"
                    >
                        <Sun v-if="isDarkTheme" :size="18" />
                        <Moon v-else :size="18" />
                    </button>
                    <button
                        v-if="currentUser"
                        class="auth-action is-signed"
                        type="button"
                        title="刷新云端历史"
                        @click="loadRemoteHistory"
                    >
                        <UserRound :size="16" />
                        <span>{{ currentUser.display_name }}</span>
                    </button>
                    <button
                        v-if="currentUser"
                        class="icon-button"
                        type="button"
                        aria-label="退出登录"
                        title="退出登录"
                        @click="logout"
                    >
                        <LogOut :size="17" />
                    </button>
                    <button
                        v-else
                        class="auth-action"
                        type="button"
                        @click="openAuthModal('login')"
                    >
                        <LogIn :size="16" />
                        <span>登录</span>
                    </button>
                    <button class="outline-action is-active" type="button">
                        平台指南
                    </button>
                    <a
                        class="solid-action github-action"
                        :href="GITHUB_REPO_URL"
                        target="_blank"
                        rel="noreferrer"
                        aria-label="打开 Neko AI Search GitHub 仓库"
                    >
                        <svg
                            class="github-mark"
                            viewBox="0 0 24 24"
                            aria-hidden="true"
                            focusable="false"
                        >
                            <path
                                fill="currentColor"
                                d="
                                    M12 2C6.48 2 2 6.58 2 12.22c0 4.52 2.87 8.35 6.84 9.7
                                    .5.1.68-.22.68-.5v-1.75c-2.78.62-3.37-1.37-3.37-1.37
                                    -.45-1.18-1.11-1.5-1.11-1.5-.91-.63.07-.62.07-.62
                                    1 .07 1.53 1.06 1.53 1.06.9 1.56 2.35 1.11 2.92.85
                                    .09-.66.35-1.11.63-1.36-2.22-.26-4.56-1.14-4.56-5.05
                                    0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71
                                    0 0 .84-.27 2.75 1.05A9.36 9.36 0 0 1 12 6.93
                                    c.85 0 1.7.12 2.5.34 1.9-1.32 2.74-1.05 2.74-1.05
                                    .55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75
                                    0 3.92-2.34 4.78-4.57 5.03.36.32.68.94.68 1.9v2.81
                                    c0 .28.18.6.69.5A10.11 10.11 0 0 0 22 12.22
                                    C22 6.58 17.52 2 12 2Z
                                "
                            />
                        </svg>
                        <span>GitHub</span>
                        <ArrowUpRight :size="15" aria-hidden="true" />
                    </a>
                </div>
            </header>
            <section id="platform-guide" class="platform-guide" aria-labelledby="guide-title">
                <div class="guide-intro">
                    <p class="guide-kicker">Platform guide</p>
                    <h2 id="guide-title">从提问到溯源，一次看懂 Neko AI Search</h2>
                    <p>
                        平台会围绕你的问题完成多源检索、AI 综合回答、引用来源整理，
                        并把图片、文件、视频和网页结果分区呈现。
                    </p>
                </div>

                <div class="guide-flow" aria-label="平台功能流程图">
                    <div class="flow-node">
                        <span><Search :size="18" /></span>
                        <strong>输入问题</strong>
                        <small>快速 / 深度</small>
                    </div>
                    <i aria-hidden="true" />
                    <div class="flow-node">
                        <span><Globe2 :size="18" /></span>
                        <strong>多源检索</strong>
                        <small>网页、图片、视频</small>
                    </div>
                    <i aria-hidden="true" />
                    <div class="flow-node">
                        <span><Sparkles :size="18" /></span>
                        <strong>AI 综合</strong>
                        <small>流式生成回答</small>
                    </div>
                    <i aria-hidden="true" />
                    <div class="flow-node">
                        <span><ShieldCheck :size="18" /></span>
                        <strong>引用溯源</strong>
                        <small>查看可信来源</small>
                    </div>
                </div>

                <div class="guide-showcase" aria-label="平台界面截图示意">
                    <article class="guide-snapshot is-search-shot">
                        <div class="snapshot-topline">
                            <span>首页搜索</span>
                            <strong>01</strong>
                        </div>
                        <div class="snapshot-search-box">
                            <Search :size="18" />
                            <span class="snapshot-query-text">国产 AI 大模型有哪些？</span>
                            <span class="snapshot-submit" aria-hidden="true">
                                <ArrowUp :size="16" />
                            </span>
                        </div>
                        <div class="snapshot-prompt-grid">
                            <span>DeepSeek V4 有哪些能力？</span>
                            <span>AI 如何改变未来工作方式？</span>
                            <span>量子计算的最新进展</span>
                            <span>什么是 Spring AI？</span>
                        </div>
                    </article>

                    <article class="guide-snapshot is-result-shot">
                        <div class="snapshot-topline">
                            <span>结果页解析</span>
                            <strong>02</strong>
                        </div>
                        <div class="snapshot-layout">
                            <div class="snapshot-answer">
                                <span class="shot-label">AI 综合回答</span>
                                <strong>国产 AI 大模型主要覆盖通用、行业与多模态场景。</strong>
                                <p>回答会保留引用编号，便于回到来源核验。</p>
                                <div class="shot-lines">
                                    <span />
                                    <span />
                                    <span />
                                </div>
                            </div>
                            <div class="snapshot-results">
                                <span class="shot-label">完整搜索结果</span>
                                <div>
                                    <Image :size="16" />
                                    <span>图片资源</span>
                                </div>
                                <div>
                                    <Layers3 :size="16" />
                                    <span>网页 / 视频 / 文件</span>
                                </div>
                            </div>
                        </div>
                    </article>
                </div>

                <section class="mode-guide-panel" aria-labelledby="mode-guide-title">
                    <div class="mode-guide-heading">
                        <p class="guide-kicker">Search modes</p>
                        <h3 id="mode-guide-title">快速模式和深度模式有什么区别？</h3>
                        <p>
                            两种模式都会执行安全检查、多源检索、流式回答和引用溯源；
                            区别在于 AI 使用的来源上下文、回答详略和相关问题生成方式。
                        </p>
                    </div>

                    <div class="mode-guide-grid">
                        <article class="mode-guide-card is-fast">
                            <div class="mode-card-title">
                                <span><Bolt :size="18" /></span>
                                <div>
                                    <strong>快速模式</strong>
                                    <small>默认推荐，优先更快得到结论</small>
                                </div>
                            </div>
                            <ul>
                                <li>适合定义查询、事实确认、新闻概览和轻量对比。</li>
                                <li>使用较紧凑的来源上下文，优先输出直接结论和关键证据。</li>
                                <li>相关问题优先本地规则生成，减少额外模型调用耗时。</li>
                            </ul>
                        </article>

                        <article class="mode-guide-card is-deep">
                            <div class="mode-card-title">
                                <span><Sparkles :size="18" /></span>
                                <div>
                                    <strong>深度模式</strong>
                                    <small>适合复杂问题，优先更完整的分析</small>
                                </div>
                            </div>
                            <ul>
                                <li>适合方案调研、长问题、争议信息和需要多来源核验的任务。</li>
                                <li>会给 AI 更多来源和更长内容摘要，回答更完整、结构更充分。</li>
                                <li>相关问题会结合回答内容生成，更适合继续追问和延展探索。</li>
                            </ul>
                        </article>
                    </div>
                </section>

                <div class="guide-lesson-grid" aria-label="平台使用步骤">
                    <article class="guide-lesson">
                        <span class="lesson-index">Step 01</span>
                        <h3>输入一个需要验证的问题</h3>
                        <p>在首页搜索框输入问题，短问题建议使用快速模式，复杂选题可切换深度模式。</p>
                    </article>
                    <article class="guide-lesson">
                        <span class="lesson-index">Step 02</span>
                        <h3>观察搜索过程</h3>
                        <p>结果页会展示限流、安全、缓存、检索、生成等节点，让搜索链路更透明。</p>
                    </article>
                    <article class="guide-lesson">
                        <span class="lesson-index">Step 03</span>
                        <h3>阅读带引用的 AI 回答</h3>
                        <p>答案中的引用编号会对应来源列表，点击即可打开原始网页进行核验。</p>
                    </article>
                    <article class="guide-lesson">
                        <span class="lesson-index">Step 04</span>
                        <h3>继续查看分类结果</h3>
                        <p>图片资源会独立展示，网页、视频和文件进入完整结果窗口，避免信息混在一起。</p>
                    </article>
                </div>
            </section>
        </section>

        <section v-else class="results-view" aria-label="搜索结果">
            <!-- 结果页顶部栏保留搜索入口和主题切换。 -->
            <header class="topbar">
                <div class="topbar-inner">
                    <button
                        class="brand-button"
                        type="button"
                        @click="showHomePage"
                    >
                        <img :src="logoUrl" alt="" />
                        <span class="brand-name">Neko AI Search</span>
                    </button>
                    <form class="compact-search" @submit.prevent="submitSearch()">
                        <Search class="search-box-icon" :size="18" aria-hidden="true" />
                        <input
                            v-model="query"
                            autocomplete="off"
                            aria-label="继续搜索"
                            placeholder="继续搜索一个新问题"
                        />
                        <div class="mode-toggle" aria-label="搜索模式">
                            <button
                                type="button"
                                :class="{ 'is-active': searchMode === 'fast' }"
                                @click="searchMode = 'fast'"
                            >
                                快速
                            </button>
                            <button
                                type="button"
                                :class="{ 'is-active': searchMode === 'deep' }"
                                @click="searchMode = 'deep'"
                            >
                                深度
                            </button>
                        </div>
                        <button
                            type="submit"
                            aria-label="搜索"
                            title="搜索"
                            :disabled="isSearching || query.trim().length < 2"
                        >
                            <Loader2 v-if="isSearching" class="spin" :size="18" />
                            <ArrowUp v-else :size="18" aria-hidden="true" />
                            <span class="sr-only">搜索</span>
                        </button>
                    </form>
                    <div class="topbar-actions">
                        <button
                            v-if="currentUser"
                            class="auth-action is-signed"
                            type="button"
                            title="刷新云端历史"
                            @click="loadRemoteHistory"
                        >
                            <UserRound :size="16" />
                            <span>{{ currentUser.display_name }}</span>
                        </button>
                        <button
                            v-if="currentUser"
                            class="icon-button"
                            type="button"
                            aria-label="退出登录"
                            title="退出登录"
                            @click="logout"
                        >
                            <LogOut :size="17" />
                        </button>
                        <button
                            v-else
                            class="auth-action"
                            type="button"
                            @click="openAuthModal('login')"
                        >
                            <LogIn :size="16" />
                            <span>登录</span>
                        </button>
                        <button
                            class="icon-button"
                            type="button"
                            :aria-label="themeToggleLabel"
                            :title="themeToggleLabel"
                            @click="toggleTheme"
                        >
                            <Sun v-if="isDarkTheme" :size="18" />
                            <Moon v-else :size="18" />
                        </button>
                    </div>
                </div>
            </header>

            <div class="content-grid">
                <aside class="side-rail" aria-label="历史记录">
                    <div class="side-title">
                        <Clock3 :size="16" />
                        <span>历史</span>
                    </div>
                    <button
                        v-for="item in history"
                        :key="item.id"
                        type="button"
                        class="history-row"
                        @click="submitSearch(item.query)"
                    >
                        <RotateCcw :size="15" />
                        <span>{{ item.query }}</span>
                    </button>
                    <div class="side-title history-title">
                        <div>
                            <Clock3 :size="16" />
                            <span>历史</span>
                        </div>
                        <button
                            v-if="currentUser && history.length"
                            type="button"
                            title="清空历史"
                            aria-label="清空历史"
                            @click="clearCurrentHistory"
                        >
                            <Trash2 :size="14" />
                        </button>
                    </div>
                    <div class="history-scope">
                        <strong>{{ historyScopeLabel }}</strong>
                        <span>{{ historyHint }}</span>
                    </div>
                    <p v-if="historyLoading" class="history-empty">正在同步历史...</p>
                    <p v-else-if="!history.length" class="history-empty">
                        {{ currentUser ? "暂无云端历史记录" : "登录后自动保存你的搜索记录" }}
                    </p>
                    <button
                        v-if="!currentUser"
                        class="history-login-action"
                        type="button"
                        @click="openAuthModal('login')"
                    >
                        <LogIn :size="15" />
                        <span>登录保存历史</span>
                    </button>
                    <template v-if="history.length">
                        <div
                            v-for="item in history"
                            :key="item.id"
                            class="history-row"
                        >
                            <button type="button" @click="submitSearch(item.query)">
                                <RotateCcw :size="15" />
                                <span>{{ item.query }}</span>
                            </button>
                            <button
                                type="button"
                                title="删除这条历史"
                                aria-label="删除这条历史"
                                @click="removeHistoryItem(item)"
                            >
                                <X :size="14" />
                            </button>
                        </div>
                    </template>
                    <section class="official-qr-card" aria-label="平台公众号二维码">
                        <div class="official-qr-heading">
                            <img :src="logoUrl" alt="" />
                            <div>
                                <span>平台公众号</span>
                                <strong>Neko AI Search</strong>
                            </div>
                        </div>
                        <div class="official-qr-image">
                            <img :src="officialQrUrl" alt="Neko AI Search 平台公众号二维码" />
                        </div>
                        <p>扫码关注，获取产品更新与 AI 搜索技巧。</p>
                    </section>
                </aside>

                <section class="answer-column" aria-live="polite">
                    <p class="query-label">Question</p>
                    <h2>{{ activeQuery }}</h2>

                    <div v-if="errorMessage" class="error-box" role="alert">
                        {{ errorMessage }}
                    </div>

                    <section
                        v-if="searchId || traceSteps.length"
                        class="trace-panel"
                        aria-label="搜索过程"
                    >
                        <div class="trace-header">
                            <span>搜索过程</span>
                            <code v-if="searchId">{{ searchId }}</code>
                        </div>
                        <div class="trace-list">
                            <div
                                v-for="step in traceSteps"
                                :key="step.name"
                                :class="['trace-row', `is-${step.status}`]"
                            >
                                <Loader2
                                    v-if="step.status === 'running'"
                                    class="spin"
                                    :size="15"
                                />
                                <CheckCircle2 v-else-if="step.status === 'success'" :size="15" />
                                <CircleAlert v-else :size="15" />
                                <span>{{ step.label }}</span>
                                <strong v-if="step.duration_ms !== undefined">
                                    {{ formatDuration(step.duration_ms) }}
                                </strong>
                                <small v-if="step.error_message">
                                    {{ step.error_message }}
                                </small>
                            </div>
                        </div>
                    </section>

                    <div class="answer-surface">
                        <div class="section-heading">
                            <Sparkles :size="18" />
                            <span>AI 综合回答</span>
                            <Loader2 v-if="isSearching" class="spin muted" :size="18" />
                        </div>
                        <div v-if="answer" class="markdown-body" v-html="renderedAnswer" />
                        <div
                            v-else-if="isSearching"
                            class="skeleton-stack"
                            aria-label="正在生成回答"
                        >
                            <span />
                            <span />
                            <span />
                        </div>
                        <p v-else class="empty-answer">
                            暂时没有生成回答，请确认后端服务可用后重试。
                        </p>
                    </div>

                    <section v-if="referencedSources.length" class="sources-section">
                        <h3>引用来源</h3>
                        <a
                            v-for="source in referencedSources"
                            :key="source.id"
                            class="source-link"
                            :href="source.url"
                        >
                            <span>[{{ source.id }}]</span>
                            <strong>{{ source.title }}</strong>
                            <ArrowUpRight :size="16" />
                        </a>
                    </section>

                    <section v-if="relatedQuestions.length" class="related-section">
                        <h3>相关问题</h3>
                        <button
                            v-for="question in relatedQuestions"
                            :key="question"
                            type="button"
                            @click="submitSearch(question)"
                        >
                            {{ question }}
                        </button>
                    </section>
                </section>

                <section class="results-column">
                    <div class="results-scroll-window">
                    <section v-if="standaloneImageResults.length" class="image-results-panel">
                        <div class="result-section-heading">
                            <div>
                                <span>图片资源</span>
                                <strong>{{ standaloneImageResults.length }}</strong>
                            </div>
                            <button
                                v-if="standaloneImageResults.length > 4"
                                type="button"
                                @click="showAllImages = !showAllImages"
                            >
                                {{ showAllImages ? "收起图片" : "查看更多图片" }}
                            </button>
                        </div>

                        <div class="image-resource-grid">
                            <a
                                v-for="result in visibleImageResults"
                                :key="result.id"
                                class="image-resource-card"
                                :href="result.url"
                                target="_blank"
                                rel="noreferrer"
                            >
                                <img
                                    v-if="mediaPreviewSrc(result)"
                                    :src="mediaPreviewSrc(result)"
                                    :alt="result.title"
                                    loading="lazy"
                                    @error="handlePreviewError(result.id)"
                                />
                                <span v-else class="image-resource-fallback">
                                    <Image :size="22" />
                                </span>
                                <span class="image-resource-title">{{ result.title }}</span>
                            </a>
                        </div>
                    </section>

                    <div class="result-section-heading">
                        <div>
                            <span>完整搜索结果</span>
                            <strong>{{ listedSearchResults.length }}</strong>
                        </div>
                    </div>
                    <article
                        v-for="result in listedSearchResults"
                        :key="result.id"
                        :class="['result-card', `is-${result.type}`]"
                        role="link"
                        tabindex="0"
                        @click="openExternal(result.url)"
                        @keydown.enter="openExternal(result.url)"
                    >
                        <div v-if="result.type === 'file'" class="file-preview">
                            <FileText :size="28" />
                            <div>
                                <strong>{{ fileTypeLabel(result) }}</strong>
                                <span>{{ hostLabel(result.url) }}</span>
                            </div>
                        </div>
                        <div
                            v-else-if="mediaPreviewSrc(result)"
                            :class="[
                                'result-preview',
                                { 'is-video-preview': result.type === 'video' },
                            ]"
                        >
                            <img
                                :src="mediaPreviewSrc(result)"
                                :alt="result.title"
                                loading="lazy"
                                @error="handlePreviewError(result.id)"
                            />
                            <span v-if="result.type === 'video'" class="preview-play">
                                <PlayCircle :size="24" />
                            </span>
                        </div>
                        <div
                            v-else-if="result.type === 'video'"
                            class="result-preview icon-preview"
                        >
                            <PlayCircle :size="34" />
                        </div>
                        <div class="result-meta">
                            <span>[{{ result.id }}]</span>
                            <span class="type-pill">
                                <Image v-if="result.type === 'image'" :size="13" />
                                <PlayCircle v-else-if="result.type === 'video'" :size="13" />
                                <FileText v-else-if="result.type === 'file'" :size="13" />
                                <Globe2 v-else :size="13" />
                                {{ resultTypeLabel(result) }}
                            </span>
                            <span>{{ hostLabel(result.url) }}</span>
                            <span v-if="result.score">匹配 {{ result.score.toFixed(2) }}</span>
                        </div>
                        <a :href="result.url" @click.stop>
                            {{ result.title }}
                            <ArrowUpRight :size="15" />
                        </a>
                        <p>{{ result.content }}</p>
                    </article>
                    </div>
                </section>
            </div>
        </section>

        <div
            v-if="isAuthModalOpen"
            class="auth-overlay"
            role="presentation"
            @click.self="closeAuthModal"
        >
            <section class="auth-dialog" role="dialog" aria-modal="true" aria-labelledby="auth-title">
                <button
                    class="auth-close"
                    type="button"
                    aria-label="关闭登录窗口"
                    @click="closeAuthModal"
                >
                    <X :size="18" />
                </button>

                <div class="auth-dialog-head">
                    <span class="auth-emblem">
                        <img :src="logoUrl" alt="" />
                    </span>
                    <div>
                        <p>{{ authMode === "login" ? "Session Cookie" : "Private account" }}</p>
                        <h2 id="auth-title">
                            {{ authMode === "login" ? "登录 Neko AI Search" : "创建私有账号" }}
                        </h2>
                    </div>
                </div>

                <div class="auth-tabs" aria-label="认证方式">
                    <button
                        type="button"
                        :class="{ 'is-active': authMode === 'login' }"
                        @click="switchAuthMode('login')"
                    >
                        登录
                    </button>
                    <button
                        type="button"
                        :class="{ 'is-active': authMode === 'register' }"
                        @click="switchAuthMode('register')"
                    >
                        注册
                    </button>
                </div>

                <form class="auth-form" @submit.prevent="submitAuthForm">
                    <label v-if="authMode === 'register'">
                        <span>昵称</span>
                        <input
                            v-model="authDisplayName"
                            autocomplete="name"
                            maxlength="80"
                            placeholder="例如 Neko Explorer"
                        />
                    </label>
                    <label>
                        <span>邮箱</span>
                        <input
                            v-model="authEmail"
                            autocomplete="email"
                            inputmode="email"
                            placeholder="you@example.com"
                            required
                        />
                    </label>
                    <label>
                        <span>密码</span>
                        <input
                            v-model="authPassword"
                            autocomplete="current-password"
                            minlength="8"
                            maxlength="128"
                            type="password"
                            placeholder="至少 8 位字符"
                            required
                        />
                    </label>
                    <p v-if="authError" class="auth-error">{{ authError }}</p>
                    <button class="auth-submit" type="submit" :disabled="authBusy">
                        <Loader2 v-if="authBusy" class="spin" :size="17" />
                        <LogIn v-else :size="17" />
                        <span>{{ authMode === "login" ? "登录" : "注册并登录" }}</span>
                    </button>
                </form>

                <p class="auth-footnote">
                    登录后历史记录将按账号隔离保存，当前步骤暂不启用积分配额。
                </p>
            </section>
        </div>
    </main>
</template>
