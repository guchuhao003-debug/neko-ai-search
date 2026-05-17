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
    Clock3,
    Loader2,
    RotateCcw,
    Search,
    Sparkles,
} from "@lucide/vue";
import type { SearchHistoryItem, SearchResult, SseFrame } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const HISTORY_KEY = "neko-ai-search-history";

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
const answer = ref("");
const errorMessage = ref("");
const isSearching = ref(false);
const searchResults = ref<SearchResult[]>([]);
const relatedQuestions = ref<string[]>([]);
const history = ref<SearchHistoryItem[]>(loadHistory());
const hotSearches = ["DeepSeek V4 有哪些能力？", "国产 AI 大模型有哪些？"];

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
    isSearching.value = true;
    rememberQuery(trimmed);

    try {
        const response = await fetch(`${API_BASE_URL}/api/search/stream`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Accept: "text/event-stream",
            },
            body: JSON.stringify({ query: trimmed }),
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
        errorMessage.value = data.message;
    }
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
</script>

<template>
    <main :class="['app-shell', { 'is-results': hasSearched }]">
        <section v-if="!hasSearched" class="home-view" aria-labelledby="home-title">
            <div class="brand-mark">
                <Sparkles class="brand-icon" :size="42" />
                <h1 id="home-title">Neko AI Search</h1>
            </div>
            <form class="hero-search" @submit.prevent="submitSearch()">
                <label class="sr-only" for="home-search">输入你的问题</label>
                <div class="search-box">
                    <Search class="search-box-icon" :size="19" aria-hidden="true" />
                    <input
                        id="home-search"
                        v-model="query"
                        autocomplete="off"
                        placeholder="智能 AI 搜索资讯、信息"
                    />
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
                    <Clock3 :size="15" />
                    {{ item }}
                </button>
            </div>
        </section>

        <section v-else class="results-view" aria-label="搜索结果">
            <header class="topbar">
                <div class="topbar-inner">
                    <button
                        class="brand-button"
                        type="button"
                        @click="activeQuery = ''; answer = ''"
                    >
                        <Sparkles class="brand-icon" :size="32" />
                        <span>Neko AI Search</span>
                    </button>
                    <form class="compact-search" @submit.prevent="submitSearch()">
                        <Search class="search-box-icon" :size="19" aria-hidden="true" />
                        <input
                            v-model="query"
                            autocomplete="off"
                            aria-label="继续搜索"
                            placeholder="智能 AI 搜索资讯、信息"
                        />
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
                        class="result-card"
                        role="link"
                        tabindex="0"
                        @click="openExternal(result.url)"
                        @keydown.enter="openExternal(result.url)"
                    >
                        <div class="result-meta">
                            <span>[{{ result.id }}]</span>
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
