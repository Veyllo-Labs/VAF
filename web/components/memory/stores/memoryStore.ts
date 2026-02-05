/**
 * Zustand store for VAF Memory System state management.
 * 
 * Manages:
 * - Memory graph nodes and edges
 * - Selected memory state
 * - RAG query results and sources
 * - API interactions
 */

import { create } from 'zustand';

// Types
export interface MemoryMetadata {
    title: string;
    preview?: string;
    tags?: string[];
    type?: string;
    source?: string;
    created_at?: string;
}

export interface Memory {
    id: string;
    user_scope_id?: string | null;
    metadata: MemoryMetadata;
    parent_id: string | null;
    created_at: string | null;
    updated_at: string | null;
    chunk_count: number;
    content?: string;
}

export interface MemoryNode {
    id: string;
    type: string;  // 'memoryNode' | 'tagNode'
    position: { x: number; y: number };
    data: {
        label: string;
        // Memory node specific fields
        tags?: string[];
        preview?: string;
        type?: string;
        createdAt?: string | null;
        updatedAt?: string | null;
        chunkCount?: number;
        isHighlighted?: boolean;
        relevance?: number;
        hasParent?: boolean;
        parentId?: string | null;
        // Tag node specific fields
        tag?: string;
        memoryCount?: number;
        isTagNode?: boolean;
    };
}

export interface MemoryEdge {
    id: string;
    source: string;
    target: string;
    type: string;
    animated: boolean;
    data: {
        strength: number;
        connectionType: string;  // 'semantic' | 'manual' | 'tag'
        label: string | null;
    };
    style: {
        strokeWidth: number;
        opacity: number;
        stroke?: string;         // Optional custom stroke color
        strokeDasharray?: string; // Optional dash pattern (e.g., "5,5" for tag edges)
    };
}

export interface RagSource {
    memory_id: string;
    chunk_id: string;
    text: string;
    score: number;
    metadata: MemoryMetadata;
}

export interface RagResult {
    answer: string;
    sources: RagSource[];
    context_tokens: number;
}

export interface MemoryStats {
    memories: number;
    chunks: number;
    connections: number;
    db_connected: boolean;
}

interface MemoryState {
    // Graph state
    nodes: MemoryNode[];
    edges: MemoryEdge[];
    
    // Selection
    selectedMemory: Memory | null;
    selectedNodeId: string | null;
    
    // RAG state
    ragQuery: string;
    ragResult: RagResult | null;
    ragSources: string[];  // Memory IDs used in last RAG query
    isQuerying: boolean;
    streamingAnswer: string;
    
    // UI state
    isLoading: boolean;
    error: string | null;
    stats: MemoryStats | null;
    
    // Filter state
    typeFilter: string | null;
    tagFilter: string | null;
    searchQuery: string;
}

interface MemoryActions {
    // Graph actions
    fetchGraph: (limit?: number) => Promise<void>;
    setNodes: (nodes: MemoryNode[]) => void;
    setEdges: (edges: MemoryEdge[]) => void;
    highlightNodes: (nodeIds: string[]) => void;
    clearHighlights: () => void;
    
    // Memory CRUD
    fetchMemory: (id: string) => Promise<Memory | null>;
    createMemory: (content: string, metadata?: Partial<MemoryMetadata>) => Promise<Memory | null>;
    updateMemory: (id: string, content?: string, metadata?: Partial<MemoryMetadata>) => Promise<Memory | null>;
    deleteMemory: (id: string, hard?: boolean) => Promise<boolean>;
    
    // Selection
    selectMemory: (id: string | null) => void;
    setSelectedNodeId: (id: string | null) => void;
    
    // RAG actions
    setRagQuery: (query: string) => void;
    queryRag: (query: string, k?: number) => Promise<void>;
    queryRagStream: (query: string, k?: number) => Promise<void>;
    clearRagResult: () => void;
    
    // Stats
    fetchStats: () => Promise<void>;
    
    // Filters
    setTypeFilter: (type: string | null) => void;
    setTagFilter: (tag: string | null) => void;
    setSearchQuery: (query: string) => void;
    
    // Error handling
    setError: (error: string | null) => void;
    clearError: () => void;
}

// Use relative /api in browser so Next.js rewrite proxies to backend (same-origin, no CORS).
// Resolve at call time so browser requests use '' and SSR can use absolute URL.
function getMemoryApiBase(): string {
  if (typeof window !== 'undefined') return '';
  return process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001';
}

export const useMemoryStore = create<MemoryState & MemoryActions>((set, get) => ({
    // Initial state
    nodes: [],
    edges: [],
    selectedMemory: null,
    selectedNodeId: null,
    ragQuery: '',
    ragResult: null,
    ragSources: [],
    isQuerying: false,
    streamingAnswer: '',
    isLoading: false,
    error: null,
    stats: null,
    typeFilter: null,
    tagFilter: null,
    searchQuery: '',
    
    // Graph actions
    fetchGraph: async (limit = 100) => {
        set({ isLoading: true, error: null });
        try {
            const { ragSources } = get();
            const highlightParam = ragSources.length > 0 ? `&highlight=${ragSources.join(',')}` : '';
            
            const base = getMemoryApiBase();
            const response = await fetch(`${base}/api/memory/graph?limit=${limit}${highlightParam}`);
            if (!response.ok) throw new Error('Failed to fetch graph');
            
            const data = await response.json();
            set({ nodes: data.nodes, edges: data.edges, isLoading: false });
        } catch (error) {
            set({ error: (error as Error).message, isLoading: false });
        }
    },
    
    setNodes: (nodes) => set({ nodes }),
    setEdges: (edges) => set({ edges }),
    
    highlightNodes: (nodeIds) => {
        const { nodes } = get();
        const highlightSet = new Set(nodeIds);
        
        const updatedNodes = nodes.map(node => ({
            ...node,
            data: {
                ...node.data,
                isHighlighted: highlightSet.has(node.id)
            }
        }));
        
        set({ nodes: updatedNodes, ragSources: nodeIds });
    },
    
    clearHighlights: () => {
        const { nodes } = get();
        const updatedNodes = nodes.map(node => ({
            ...node,
            data: { ...node.data, isHighlighted: false, relevance: 0 }
        }));
        set({ nodes: updatedNodes, ragSources: [] });
    },
    
    // Memory CRUD
    fetchMemory: async (id) => {
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory/${id}?include_content=true`);
            if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                const detail = (body as { detail?: string })?.detail;
                throw new Error(detail && typeof detail === 'string' ? detail : `Failed to fetch memory (${response.status})`);
            }
            const memory = await response.json();
            set({ selectedMemory: memory, error: null });
            return memory;
        } catch (error) {
            set({ error: (error as Error).message, selectedMemory: null });
            return null;
        }
    },
    
    createMemory: async (content, metadata) => {
        set({ isLoading: true, error: null });
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content, metadata, auto_connect: true })
            });
            
            if (!response.ok) throw new Error('Failed to create memory');
            
            const memory = await response.json();
            
            // Refresh graph to include new memory
            await get().fetchGraph();
            await get().fetchStats();
            
            set({ isLoading: false });
            return memory;
        } catch (error) {
            set({ error: (error as Error).message, isLoading: false });
            return null;
        }
    },
    
    updateMemory: async (id, content, metadata) => {
        set({ isLoading: true, error: null });
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content, metadata })
            });
            
            if (!response.ok) throw new Error('Failed to update memory');
            
            const memory = await response.json();
            set({ selectedMemory: memory, isLoading: false });
            
            // Refresh graph
            await get().fetchGraph();
            
            return memory;
        } catch (error) {
            set({ error: (error as Error).message, isLoading: false });
            return null;
        }
    },
    
    deleteMemory: async (id, hard = false) => {
        set({ isLoading: true, error: null });
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory/${id}?hard=${hard}`, {
                method: 'DELETE'
            });
            
            if (!response.ok) throw new Error('Failed to delete memory');
            
            // Clear selection if deleted memory was selected
            const { selectedMemory } = get();
            if (selectedMemory?.id === id) {
                set({ selectedMemory: null, selectedNodeId: null });
            }
            
            // Refresh graph
            await get().fetchGraph();
            await get().fetchStats();
            
            set({ isLoading: false });
            return true;
        } catch (error) {
            set({ error: (error as Error).message, isLoading: false });
            return false;
        }
    },
    
    // Selection
    selectMemory: async (id) => {
        if (!id) {
            set({ selectedMemory: null, selectedNodeId: null });
            return;
        }
        
        set({ selectedNodeId: id });
        await get().fetchMemory(id);
    },
    
    setSelectedNodeId: (id) => set({ selectedNodeId: id }),
    
    // RAG actions
    setRagQuery: (query) => set({ ragQuery: query }),
    
    queryRag: async (query, k = 5) => {
        set({ isQuerying: true, error: null, ragQuery: query });
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory/rag/query`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, k })
            });
            
            if (!response.ok) throw new Error('RAG query failed');
            
            const result = await response.json();
            
            // Extract memory IDs for highlighting
            const sourceIds: string[] = Array.from(new Set(result.sources.map((s: RagSource) => s.memory_id))) as string[];
            
            set({ 
                ragResult: result, 
                ragSources: sourceIds,
                isQuerying: false 
            });
            
            // Highlight source nodes in graph
            get().highlightNodes(sourceIds);
            
        } catch (error) {
            set({ error: (error as Error).message, isQuerying: false });
        }
    },
    
    queryRagStream: async (query, k = 5) => {
        set({ 
            isQuerying: true, 
            error: null, 
            ragQuery: query, 
            streamingAnswer: '',
            ragResult: null 
        });
        
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory/rag/query/stream`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, k, stream: true })
            });
            
            if (!response.ok) throw new Error('Streaming RAG query failed');
            
            const reader = response.body?.getReader();
            if (!reader) throw new Error('No response body');
            
            const decoder = new TextDecoder();
            let sources: RagSource[] = [];
            let fullAnswer = '';
            
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                
                const text = decoder.decode(value);
                const lines = text.split('\n').filter(line => line.startsWith('data: '));
                
                for (const line of lines) {
                    const data = JSON.parse(line.slice(6));
                    
                    if (data.type === 'sources') {
                        sources = data.sources;
                        const sourceIds = [...new Set(sources.map(s => s.memory_id))];
                        set({ ragSources: sourceIds });
                        get().highlightNodes(sourceIds);
                    } else if (data.type === 'token') {
                        fullAnswer += data.content;
                        set({ streamingAnswer: fullAnswer });
                    } else if (data.type === 'done') {
                        set({ 
                            ragResult: { answer: fullAnswer, sources, context_tokens: 0 },
                            isQuerying: false,
                            streamingAnswer: ''
                        });
                    } else if (data.type === 'error') {
                        throw new Error(data.error);
                    }
                }
            }
        } catch (error) {
            set({ error: (error as Error).message, isQuerying: false });
        }
    },
    
    clearRagResult: () => {
        set({ ragResult: null, ragSources: [], streamingAnswer: '' });
        get().clearHighlights();
    },
    
    // Stats
    fetchStats: async () => {
        try {
            const response = await fetch(`${getMemoryApiBase()}/api/memory/stats`);
            if (!response.ok) throw new Error('Failed to fetch stats');
            
            const stats = await response.json();
            set({ stats });
        } catch (error) {
            console.error('Failed to fetch stats:', error);
        }
    },
    
    // Filters
    setTypeFilter: (type) => set({ typeFilter: type }),
    setTagFilter: (tag) => set({ tagFilter: tag }),
    setSearchQuery: (query) => set({ searchQuery: query }),
    
    // Error handling
    setError: (error) => set({ error }),
    clearError: () => set({ error: null })
}));
