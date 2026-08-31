'use client';

import React, { useEffect, useState } from 'react';
import {
  Database,
  Search,
  Plus,
  Trash2,
  Sparkles,
  Layers,
  FileText,
  Briefcase,
  BookOpen,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
  Zap,
} from 'lucide-react';
import {
  api,
  KnowledgeDocument,
  KnowledgeStats,
  SearchKnowledgeResult,
} from '@/lib/api';

const TEMPLATES = [
  {
    id: 'star',
    label: 'STAR Behavioral Story',
    source_type: 'star_story',
    placeholderTitle: 'Handling a Major Production Outage (STAR)',
    template: `Situation: During peak holiday traffic, our primary payment processing service experienced a cascading failure and high latency.

Task: As the on-call senior backend engineer, I needed to triage the root cause, restore transaction throughput, and prevent financial loss.

Action: I isolated the bottleneck to an unindexed database query locking the transactions table. I immediately provisioned read replicas, routed non-critical queries, and deployed an optimized compound index migration.

Result: Latency dropped by 85% within 15 minutes, zero payment transactions were dropped, and I established automated query latency alerts.`,
  },
  {
    id: 'system_design',
    label: 'System Design Architecture',
    source_type: 'system_design',
    placeholderTitle: 'High-Throughput Notification Service Architecture',
    template: `Architecture Overview:
- API Gateway routes incoming notification requests with rate limiting and authentication.
- Kafka message bus decouples ingestion from delivery workers with partitioned topics.
- Worker fleet consumes from Kafka, batches push notifications via Redis for deduplication, and sends to APNs/FCM.
- Tradeoffs: Eventual consistency chosen over strict latency for bulk marketing alerts; priority queue for OTP verification.`,
  },
  {
    id: 'project',
    label: 'Project Deep-Dive',
    source_type: 'project',
    placeholderTitle: 'Real-Time Collaborative Code Editor Project',
    template: `Project Summary:
Built a real-time web collaborative code editor using WebSockets, Operational Transformation (OT), and WebAssembly.
Key Technologies: Next.js, Rust/Wasm, WebSocket server in Go, Redis Pub/Sub.
Key Outcome: Supported 50+ concurrent typers per document with sub-20ms synchronization latency.`,
  },
];

export default function KnowledgeBasePage() {
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [filterType, setFilterType] = useState<string>('all');

  // Search Simulator State
  const [searchQuery, setSearchQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<SearchKnowledgeResult | null>(null);

  // New Document Modal State
  const [showModal, setShowModal] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newContent, setNewContent] = useState('');
  const [newSourceType, setNewSourceType] = useState('star_story');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = async () => {
    try {
      setLoading(true);
      const [docList, statData] = await Promise.all([
        api.listKnowledge().catch(() => []),
        api.getKnowledgeStats().catch(() => null),
      ]);
      setDocs(docList);
      setStats(statData);
    } catch (err: any) {
      setError(err.message || 'Failed to load knowledge base');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleApplyTemplate = (tpl: (typeof TEMPLATES)[0]) => {
    setNewSourceType(tpl.source_type);
    setNewTitle(tpl.placeholderTitle);
    setNewContent(tpl.template);
  };

  const handleCreateDocument = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTitle.trim() || !newContent.trim()) return;

    setCreating(true);
    setError(null);
    try {
      await api.createKnowledge({
        title: newTitle.trim(),
        content: newContent.trim(),
        source_type: newSourceType,
      });
      setShowModal(false);
      setNewTitle('');
      setNewContent('');
      await loadData();
    } catch (err: any) {
      setError(err.message || 'Failed to add knowledge document');
    } finally {
      setCreating(false);
    }
  };

  const handleDeleteDocument = async (id: number) => {
    if (!confirm('Are you sure you want to remove this knowledge document and its vector embeddings?')) return;
    try {
      await api.deleteKnowledge(id);
      setDocs((prev) => prev.filter((d) => d.id !== id));
      if (stats) {
        setStats({
          ...stats,
          total_documents: Math.max(0, stats.total_documents - 1),
        });
      }
    } catch (err: any) {
      alert(err.message || 'Failed to delete');
    }
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) return;

    setSearching(true);
    try {
      const res = await api.searchKnowledge(searchQuery.trim(), 4, 0.0);
      setSearchResults(res);
    } catch (err: any) {
      console.error(err);
    } finally {
      setSearching(false);
    }
  };

  const filteredDocs = docs.filter((d) => {
    if (filterType === 'all') return true;
    return d.source_type === filterType;
  });

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Top Banner & Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white flex items-center gap-2.5">
            <Database className="w-6 h-6 text-indigo-400" />
            <span>Candidate Knowledge Base & RAG</span>
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Embed your verified experiences, STAR stories, and system architecture notes for hyper-personalized interview retrieval.
          </p>
        </div>

        <button
          onClick={() => setShowModal(true)}
          className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold transition-all shadow-md shadow-indigo-600/20 self-start md:self-auto"
        >
          <Plus className="w-4 h-4" />
          <span>Add Knowledge Item</span>
        </button>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Documents</span>
            <BookOpen className="w-4 h-4 text-indigo-400" />
          </div>
          <p className="text-2xl font-bold text-white mt-1">{stats?.total_documents ?? docs.length}</p>
          <p className="text-[11px] text-slate-500">Resumes, STAR stories & notes</p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Vector Chunks</span>
            <Layers className="w-4 h-4 text-purple-400" />
          </div>
          <p className="text-2xl font-bold text-purple-400 mt-1">{stats?.total_chunks ?? 0}</p>
          <p className="text-[11px] text-slate-500">512-token semantic segments</p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Embedding Model</span>
            <Sparkles className="w-4 h-4 text-emerald-400" />
          </div>
          <p className="text-base font-bold text-emerald-400 mt-1 truncate">
            {stats?.embedding_model || 'all-MiniLM-L6-v2'}
          </p>
          <p className="text-[11px] text-slate-500">Local inference ({stats?.embedding_dimension || 384}d)</p>
        </div>

        <div className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Storage Engine</span>
            <Database className="w-4 h-4 text-amber-400" />
          </div>
          <p className="text-base font-bold text-amber-400 mt-1">ChromaDB</p>
          <p className="text-[11px] text-slate-500">Persistent local vector DB</p>
        </div>
      </div>

      {/* Interactive Semantic Search Simulator */}
      <div className="glass-panel-glow p-6 rounded-2xl space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Search className="w-4 h-4 text-indigo-400" />
            <h2 className="text-sm font-bold text-white uppercase tracking-wider">
              Semantic Context Retrieval Sandbox
            </h2>
          </div>
          <span className="text-[11px] text-slate-400">
            Test what candidate context AI retrieves during live & practice interviews
          </span>
        </div>

        <form onSubmit={handleSearch} className="flex gap-3">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Type a sample interview question (e.g. 'Tell me about optimizing database queries' or 'How do you lead teams?')..."
            className="flex-1 px-4 py-3 rounded-xl bg-slate-900/90 border border-slate-800 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
          />
          <button
            type="submit"
            disabled={!searchQuery.trim() || searching}
            className="flex items-center gap-2 px-5 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold transition-all disabled:opacity-50"
          >
            {searching ? (
              <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              <Search className="w-4 h-4" />
            )}
            <span>Retrieve Context</span>
          </button>
        </form>

        {searchResults && (
          <div className="space-y-3 pt-3 border-t border-slate-800/80 animate-in fade-in">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span>
                Found <span className="font-bold text-indigo-400">{searchResults.total_matches}</span> relevant knowledge chunks:
              </span>
            </div>

            {searchResults.results.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {searchResults.results.map((res, i) => (
                  <div
                    key={i}
                    className="p-4 rounded-xl bg-slate-900/80 border border-slate-800 space-y-2 hover:border-slate-700 transition-colors"
                  >
                    <div className="flex items-center justify-between text-[11px]">
                      <span className="px-2 py-0.5 rounded-md bg-indigo-500/20 text-indigo-300 font-semibold uppercase text-[10px]">
                        {res.metadata?.source_type || 'Context Chunk'}
                      </span>
                      <span className="font-mono text-emerald-400 font-semibold">
                        {Math.round(res.score * 100)}% Match
                      </span>
                    </div>
                    <p className="text-xs text-slate-300 leading-relaxed line-clamp-4">
                      {res.content}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-slate-500 text-center py-4">
                No chunks matched with sufficient semantic similarity. Try adding more stories or adjusting your search phrase.
              </p>
            )}
          </div>
        )}
      </div>

      {/* Document Library Section */}
      <div className="space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <h2 className="text-lg font-bold text-white tracking-tight">Indexed Knowledge Items</h2>

          {/* Filter Pills */}
          <div className="flex flex-wrap gap-1.5 bg-slate-900/80 p-1 rounded-xl border border-slate-800 self-start">
            {[
              { key: 'all', label: 'All Items' },
              { key: 'resume', label: 'Resume' },
              { key: 'star_story', label: 'STAR Stories' },
              { key: 'system_design', label: 'System Design' },
              { key: 'project', label: 'Projects' },
            ].map((f) => (
              <button
                key={f.key}
                onClick={() => setFilterType(f.key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                  filterType === f.key
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="text-center py-12 text-slate-500 text-sm">Loading knowledge base...</div>
        ) : filteredDocs.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {filteredDocs.map((doc) => (
              <div
                key={doc.id}
                className="glass-panel p-5 rounded-2xl border border-slate-800 space-y-3 flex flex-col justify-between hover:border-slate-700 transition-colors"
              >
                <div className="space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <span className="inline-block px-2 py-0.5 rounded-md bg-indigo-500/20 text-indigo-300 font-semibold uppercase text-[10px] mb-1">
                        {doc.source_type.replace('_', ' ')}
                      </span>
                      <h3 className="font-bold text-sm text-white">{doc.title}</h3>
                    </div>

                    <button
                      onClick={() => handleDeleteDocument(doc.id)}
                      className="p-1.5 rounded-lg text-slate-500 hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
                      title="Delete document"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>

                  <p className="text-xs text-slate-400 leading-relaxed line-clamp-3">
                    {doc.content_preview}
                  </p>
                </div>

                <div className="pt-3 border-t border-slate-800/60 flex items-center justify-between text-[11px] text-slate-500">
                  <span className="flex items-center gap-1.5">
                    <Layers className="w-3.5 h-3.5 text-purple-400" />
                    <span>{doc.chunk_count} Vector Chunks</span>
                  </span>
                  <span>{doc.created_at ? new Date(doc.created_at).toLocaleDateString() : ''}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="glass-panel p-12 rounded-2xl border border-slate-800 text-center space-y-3">
            <BookOpen className="w-10 h-10 text-slate-600 mx-auto" />
            <h3 className="font-semibold text-white">No knowledge items found</h3>
            <p className="text-xs text-slate-400 max-w-sm mx-auto">
              Add your STAR stories, system design notes, or upload a resume to populate your local vector knowledge base.
            </p>
            <button
              onClick={() => setShowModal(true)}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold mt-2"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>Add First Story</span>
            </button>
          </div>
        )}
      </div>

      {/* Add Document Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="glass-panel-glow w-full max-w-2xl p-6 rounded-2xl border border-slate-800 space-y-5 animate-in zoom-in-95 duration-200">
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-white flex items-center gap-2">
                <Plus className="w-5 h-5 text-indigo-400" />
                <span>Add Candidate Knowledge Item</span>
              </h3>
              <button
                onClick={() => setShowModal(false)}
                className="text-slate-400 hover:text-white text-sm"
              >
                ✕
              </button>
            </div>

            {/* Template Selector Buttons */}
            <div className="space-y-2">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                Quick Template Starters:
              </p>
              <div className="flex flex-wrap gap-2">
                {TEMPLATES.map((tpl) => (
                  <button
                    key={tpl.id}
                    type="button"
                    onClick={() => handleApplyTemplate(tpl)}
                    className="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 text-xs text-slate-300 hover:text-white hover:border-indigo-500/50 transition-colors"
                  >
                    ⚡ {tpl.label}
                  </button>
                ))}
              </div>
            </div>

            <form onSubmit={handleCreateDocument} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-300 mb-1">
                  Document Title
                </label>
                <input
                  type="text"
                  required
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder="e.g., Scaling Redis Caching under High Concurrency"
                  className="w-full px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-800 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-300 mb-1">
                    Category Type
                  </label>
                  <select
                    value={newSourceType}
                    onChange={(e) => setNewSourceType(e.target.value)}
                    className="w-full px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white focus:outline-none focus:border-indigo-500"
                  >
                    <option value="star_story">STAR Story (Behavioral)</option>
                    <option value="system_design">System Design Architecture</option>
                    <option value="project">Project Experience</option>
                    <option value="manual">General Technical Note</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-300 mb-1">
                  Content (Will be chunked and embedded in ChromaDB)
                </label>
                <textarea
                  rows={8}
                  required
                  value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                  placeholder="Describe the situation, technical challenges, solutions, and measurable business outcomes..."
                  className="w-full p-4 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 resize-none font-mono"
                />
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="px-4 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition-all disabled:opacity-50"
                >
                  {creating && (
                    <span className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  )}
                  <span>Index & Save</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
