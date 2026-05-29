<script setup lang="ts">
import { computed, ref } from "vue";
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
    ArrowUpRight,
    Bolt,
    CheckCircle2,
    CircleAlert,
    Clock3,
    FileText,
    Globe2,
    Image,
    Layers3,
    Loader2,
    PlayCircle,
    RotateCcw,
    Search,
    ShieldCheck,
    Sparkles,
    Sun,
} from "@lucide/vue";
import type {
    SearchHistoryItem,
    SearchMode,
    SearchResult,
    SearchTraceStep,
    SseFrame,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const HISTORY_KEY = "neko-ai-search-history";
const logoUrl = new URL("./assets/logo-nav.png", import.meta.url).href;

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
const history = ref<SearchHistoryItem[]>(loadHistory());
const hotSearches = [
    "DeepSeek V4 有哪些能力？",
    "国产 AI 大模型有哪些？",
    "AI 如何改变未来工作方式？",
    "量子计算的最新进展",
];

const hasSearched = computed(() => activeQuery.value.length > 0);
const renderedAnswer = computed(() => renderAnswerMarkdown(answer.value, searchResults.value));
const referencedSources = computed(() => pickReferencedSources(answer.value, searchResults.value));

/**
 * Load persisted local search history for quick repeat searches.
 */
function loadHistory(): SearchHistoryItem[] {
    try {
        const raw = localStorage.getItem(HISTORY_KEY);
        return raw ? JSON.parse(raw) : [];
    } catch {
        return [];
    }
}

/**
 * Persist a bounded history list in localStorage.
 */
function saveHistory(items: SearchHistoryItem[]): void {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 8)));
}

/**
 * Add a query to the top of search history, removing duplicates.
 */
function rememberQuery(value: string): void {
    const item = {
        id: crypto.randomUUID(),
        query: value,
        createdAt: new Date().toISOString(),
    };
    history.value = [item, ...history.value.filter((entry) => entry.query !== value)].slice(0, 8);
    saveHistory(history.value);
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
    answer.value = "";
    errorMessage.value = "";
    searchResults.value = [];
    relatedQuestions.value = [];
    searchId.value = "";
    traceSteps.value = [];
    isSearching.value = true;
    rememberQuery(trimmed);

    try {
        const response = await fetch(`${API_BASE_URL}/api/search/stream`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Accept: "text/event-stream",
            },
            body: JSON.stringify({ query: trimmed, mode: searchMode.value }),
        });

        if (!response.ok || !response.body) {
            throw new Error(`搜索请求失败：HTTP ${response.status}`);
        }

        await readSseStream(response.body);
    } catch (error) {
        errorMessage.value = error instanceof Error ? error.message : "搜索失败，请稍后重试。";
    } finally {
        isSearching.value = false;
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
 * Read the best visual preview URL for media result cards.
 */
function previewUrl(result: SearchResult): string {
    return result.thumbnail_url || (result.type === "image" ? result.url : "");
}
</script>

<template>
    <main :class="['app-shell', { 'is-results': hasSearched }]">
        <section
            v-if="!hasSearched"
            class="home-view"
            aria-labelledby="home-title"
        >
            <header class="home-nav" aria-label="首页导航">
                <button class="home-brand" type="button" aria-label="Neko AI Search 首页">
                    <img :src="logoUrl" alt="" />
                </button>
                <nav class="home-menu" aria-label="主导航">
                    <a class="is-active" href="#home-title">首页</a>
                    <a href="#docs">文档</a>
                    <a href="#about">关于我们</a>
                </nav>
                <div class="home-actions">
                    <button class="icon-button" type="button" aria-label="切换主题">
                        <Sun :size="22" />
                    </button>
                    <button class="login-button" type="button">登录 / 注册</button>
                </div>
            </header>

            <div class="hero-content">
                <div class="hero-title-mark">
                    <Sparkles class="brand-icon" :size="40" />
                    <h1 id="home-title">Neko AI Search</h1>
                </div>
                <p>探索更智能的搜索，发现更广阔的世界</p>

                <form class="hero-search" @submit.prevent="submitSearch()">
                    <label class="sr-only" for="home-search">输入你的问题</label>
                    <div class="search-box">
                        <Search class="search-box-icon" :size="21" aria-hidden="true" />
                        <input
                            id="home-search"
                            v-model="query"
                            autocomplete="off"
                            placeholder="智能 AI 搜索资讯、信息"
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
                            <span v-else>搜索</span>
                        </button>
                    </div>
                </form>

                <div class="history-strip" aria-label="热门搜索">
                    <span>热门搜索：</span>
                    <button
                        v-for="item in hotSearches"
                        :key="item"
                        type="button"
                        @click="submitSearch(item)"
                    >
                        <ArrowUpRight :size="15" />
                        {{ item }}
                    </button>
                </div>
            </div>

            <section class="feature-grid" aria-label="平台能力">
                <article class="feature-card is-blue">
                    <span class="feature-icon"><Search :size="24" /></span>
                    <div>
                        <h2>智能搜索</h2>
                        <p>基于 AI 理解，精准匹配你想要的答案</p>
                    </div>
                </article>
                <article class="feature-card is-green">
                    <span class="feature-icon"><Bolt :size="24" /></span>
                    <div>
                        <h2>深度分析</h2>
                        <p>深度搜索模式，提供更全面更深入的回答</p>
                    </div>
                </article>
                <article class="feature-card is-purple">
                    <span class="feature-icon"><Layers3 :size="24" /></span>
                    <div>
                        <h2>多源信息</h2>
                        <p>整合全网优质内容，呈现多维度信息</p>
                    </div>
                </article>
                <article class="feature-card is-orange">
                    <span class="feature-icon"><ShieldCheck :size="24" /></span>
                    <div>
                        <h2>安全可靠</h2>
                        <p>严格过滤低质内容，保障信息准确可信</p>
                    </div>
                </article>
            </section>
        </section>

        <section v-else class="results-view" aria-label="搜索结果">
            <header class="topbar">
                <div class="topbar-inner">
                    <button
                        class="brand-button"
                        type="button"
                        @click="activeQuery = ''; answer = ''"
                    >
                        <img :src="logoUrl" alt="" />
                    </button>
                    <form class="compact-search" @submit.prevent="submitSearch()">
                        <Search class="search-box-icon" :size="19" aria-hidden="true" />
                        <input
                            v-model="query"
                            autocomplete="off"
                            aria-label="继续搜索"
                            placeholder="智能 AI 搜索资讯、信息"
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
                        <button type="submit" :disabled="isSearching || query.trim().length < 2">
                            <Loader2 v-if="isSearching" class="spin" :size="18" />
                            <span v-else>搜索</span>
                        </button>
                    </form>
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
                </aside>

                <section class="answer-column" aria-live="polite">
                    <p class="query-label">问题</p>
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
                        <div
                            v-if="answer"
                            class="markdown-body"
                            v-html="renderedAnswer"
                        />
                        <div v-else class="skeleton-stack" aria-label="正在生成回答">
                            <span />
                            <span />
                            <span />
                        </div>
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
                    <h3>完整搜索结果</h3>
                    <article
                        v-for="result in searchResults"
                        :key="result.id"
                        :class="['result-card', `is-${result.type}`]"
                        role="link"
                        tabindex="0"
                        @click="openExternal(result.url)"
                        @keydown.enter="openExternal(result.url)"
                    >
                        <div v-if="previewUrl(result)" class="result-preview">
                            <img :src="previewUrl(result)" :alt="result.title" loading="lazy" />
                        </div>
                        <div
                            v-else-if="result.type === 'video'"
                            class="result-preview icon-preview"
                        >
                            <PlayCircle :size="34" />
                        </div>
                        <div v-else-if="result.type === 'file'" class="result-preview icon-preview">
                            <FileText :size="34" />
                            <span>{{ result.file_type ?? "FILE" }}</span>
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
                </section>
            </div>
        </section>
    </main>
</template>
