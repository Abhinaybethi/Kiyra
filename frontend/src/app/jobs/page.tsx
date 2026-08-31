'use client';

import React, { useState, useEffect } from 'react';
import {
  Briefcase,
  Plus,
  Trash2,
  Sparkles,
  CheckCircle2,
  Layers,
  Award,
  AlertCircle,
} from 'lucide-react';
import { api, JobDescription } from '@/lib/api';

export default function JobDescriptionsPage() {
  const [jobs, setJobs] = useState<JobDescription[]>([]);
  const [showAddForm, setShowAddForm] = useState(false);
  const [title, setTitle] = useState('');
  const [company, setCompany] = useState('');
  const [rawText, setRawText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadJobs = () => {
    api.listJobs().then(setJobs).catch(() => {});
  };

  useEffect(() => {
    loadJobs();
  }, []);

  const handleCreateJob = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !rawText.trim()) return;

    setLoading(true);
    setError(null);

    try {
      await api.createJob({
        title: title.trim(),
        company: company.trim() || undefined,
        raw_text: rawText.trim(),
      });
      setTitle('');
      setCompany('');
      setRawText('');
      setShowAddForm(false);
      loadJobs();
    } catch (err: any) {
      setError(err.message || 'Failed to save job description');
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this job description?')) return;
    try {
      await api.deleteJob(id);
      loadJobs();
    } catch (err: any) {
      setError(err.message || 'Delete failed');
    }
  };

  return (
    <div className="max-w-5xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-purple-500/10 border border-purple-500/20 text-xs font-semibold text-purple-400">
            <Briefcase className="w-3.5 h-3.5" />
            <span>Target Role Intelligence</span>
          </div>
          <h1 className="text-2xl md:text-3xl font-bold text-white tracking-tight">Job Descriptions & Competencies</h1>
          <p className="text-sm text-slate-400">
            Analyze target job descriptions to identify required skills, likely interview focus areas, and competency maps.
          </p>
        </div>

        <button
          onClick={() => setShowAddForm(!showAddForm)}
          className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition-all shadow-md shadow-indigo-600/20"
        >
          <Plus className="w-4 h-4" />
          <span>{showAddForm ? 'Cancel' : 'Add Target Job'}</span>
        </button>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
          {error}
        </div>
      )}

      {/* Add Form */}
      {showAddForm && (
        <form onSubmit={handleCreateJob} className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-4">
          <h2 className="text-sm font-bold text-white uppercase tracking-wider">Add New Job Description</h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Role Title *</label>
              <input
                type="text"
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g. Senior Backend Engineer"
                className="w-full px-3.5 py-2 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Company Name</label>
              <input
                type="text"
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                placeholder="e.g. Stripe, Netflix"
                className="w-full px-3.5 py-2 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-slate-300">Job Description Text *</label>
            <textarea
              required
              rows={6}
              value={rawText}
              onChange={(e) => setRawText(e.target.value)}
              placeholder="Paste the full job description, requirements, and responsibilities..."
              className="w-full p-3.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 resize-none"
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={() => setShowAddForm(false)}
              className="px-4 py-2 rounded-xl text-xs text-slate-400 hover:text-white"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading}
              className="flex items-center gap-2 px-5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition-all disabled:opacity-50"
            >
              {loading && <span className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />}
              <span>Analyze & Ingest JD</span>
            </button>
          </div>
        </form>
      )}

      {/* Jobs List */}
      <div className="space-y-6">
        {jobs.length > 0 ? (
          jobs.map((jd) => (
            <div key={jd.id} className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-6">
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-base font-bold text-white">{jd.title}</h2>
                    {jd.is_active && (
                      <span className="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 text-[10px] font-bold uppercase">
                        Active Focus
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-indigo-400 font-medium">{jd.company || 'Company'}</p>
                </div>

                <button
                  onClick={() => handleDelete(jd.id)}
                  className="text-slate-500 hover:text-rose-400 p-2 rounded-lg hover:bg-slate-900 transition-colors"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>

              {/* Parsed Competency Map & Skills */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6 pt-4 border-t border-slate-800/80">
                {/* Required Skills */}
                <div className="space-y-2">
                  <p className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Award className="w-3.5 h-3.5 text-indigo-400" />
                    <span>Required Technical Competencies</span>
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {jd.parsed_data?.required_skills && jd.parsed_data.required_skills.length > 0 ? (
                      jd.parsed_data.required_skills.map((skill, idx) => (
                        <span
                          key={idx}
                          className="px-2.5 py-1 rounded-lg bg-indigo-600/15 border border-indigo-500/30 text-indigo-300 text-xs font-medium"
                        >
                          {skill}
                        </span>
                      ))
                    ) : (
                      <span className="text-xs text-slate-500">Analysis in progress...</span>
                    )}
                  </div>
                </div>

                {/* Likely Interview Focus Areas */}
                <div className="space-y-2">
                  <p className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Sparkles className="w-3.5 h-3.5 text-purple-400" />
                    <span>Expected Interview Focus Areas</span>
                  </p>
                  <ul className="space-y-1 text-xs text-slate-300">
                    {jd.parsed_data?.responsibilities && jd.parsed_data.responsibilities.length > 0 ? (
                      jd.parsed_data.responsibilities.slice(0, 4).map((resp, i) => (
                        <li key={i} className="flex items-start gap-2">
                          <span className="text-indigo-400">•</span>
                          <span>{resp}</span>
                        </li>
                      ))
                    ) : (
                      <span className="text-xs text-slate-500">General role focus areas</span>
                    )}
                  </ul>
                </div>
              </div>
            </div>
          ))
        ) : (
          <div className="glass-panel p-8 rounded-2xl border border-slate-800 text-center space-y-2 text-slate-500">
            <AlertCircle className="w-8 h-8 mx-auto text-slate-600" />
            <p className="text-sm">No job descriptions added yet.</p>
            <p className="text-xs">Add a job description above to tailor practice questions and live STAR assist.</p>
          </div>
        )}
      </div>
    </div>
  );
}
