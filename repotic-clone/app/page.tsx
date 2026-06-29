"use client";

import { useState, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Upload, Download, CheckCircle2, AlertCircle,
  Database, Building2, FileSpreadsheet, Lock, KeyRound,
} from "lucide-react";
import Link from "next/link";

// ─── Navbar ────────────────────────────────────────────────────────────────
const Navbar = () => (
  <nav className="fixed top-0 left-0 right-0 z-50 bg-white border-b border-slate-200">
    <div className="max-w-5xl mx-auto px-6 md:px-10 h-20 flex justify-between items-center">
      <Link href="/" className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-blue-600 flex items-center justify-center">
          <span className="text-white font-black text-xl">M.T</span>
        </div>
        <span className="font-extrabold text-2xl tracking-tight text-slate-900">
          Makhantaxwell<span className="text-blue-600">.</span>
        </span>
      </Link>
      <div className="flex items-center gap-6">
        <Link 
          href="/download" 
          className="bg-blue-600 text-white font-bold text-sm px-6 py-2.5 rounded-xl hover:bg-blue-700 transition-all shadow-lg shadow-blue-100 active:scale-95"
        >
          Download TCP
        </Link>
      </div>
    </div>
  </nav>
);

// ─── Footer ────────────────────────────────────────────────────────────────
const Footer = () => (
  <footer className="bg-white border-t border-slate-100 py-12">
    <div className="max-w-5xl mx-auto px-6 md:px-10 text-center">
      <div className="flex items-center justify-center gap-2 mb-4">
        <div className="w-6 h-6 rounded bg-slate-900 flex items-center justify-center text-[10px] text-white font-bold">M.T</div>
        <span className="font-bold text-slate-900">makhantaxwell</span>
      </div>
      <p className="text-slate-400 text-sm font-medium mb-6">
        © {new Date().getFullYear()} makhantaxwell Automation. All rights reserved.
      </p>
      <div className="flex justify-center gap-8 text-sm font-bold text-slate-300">
        <a href="#" className="hover:text-blue-600">Privacy</a>
        <a href="#" className="hover:text-blue-600">Terms</a>
      </div>
    </div>
  </footer>
);

// ─── Main Page ─────────────────────────────────────────────────────────────
export default function Home() {
  const [transactions, setTransactions] = useState<any[]>([]);
  const [bankLedger, setBankLedger] = useState("HDFC Bank");
  const [suspenseLedger, setSuspenseLedger] = useState("Suspense A/c");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [useAI, setUseAI] = useState(false); // New AI Toggle State

  // ─── Password-protected PDF state ────────────────────────────────────────
  const [needsPassword, setNeedsPassword] = useState(false);
  const [pdfPassword, setPdfPassword] = useState("");
  const [pendingFile, setPendingFile] = useState<File | null>(null);

  const converterRef = useRef<HTMLDivElement>(null);

  const scrollToConverter = () =>
    converterRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });

  // ─── Shared extraction logic ──────────────────────────────────────────────
  const extractFromFile = async (file: File, password?: string) => {
    setLoading(true);
    setError(null);
    setTransactions([]);
    setNeedsPassword(false);

    const formData = new FormData();
    formData.append("file", file);
    if (password) formData.append("password", password);

    const isLocal = typeof window !== "undefined" && window.location.hostname === "localhost";
    let API_URL =
      process.env.NEXT_PUBLIC_API_URL ||
      (isLocal ? "http://127.0.0.1:8000" : "https://pdf-to-xml-474c.onrender.com");
    API_URL = API_URL.replace(/\/+$/, "");
    
    // Switch endpoint based on useAI state
    const ENDPOINT = useAI 
      ? `${API_URL}/extract-statement-ai/`
      : `${API_URL}/extract-statement/`;

    try {
      const res = await fetch(ENDPOINT, { method: "POST", body: formData });

      // Handle wrong password (HTTP 401)
      if (res.status === 401) {
        const err = await res.json().catch(() => ({ detail: "Incorrect password." }));
        setNeedsPassword(true);   // keep password form visible
        setPdfPassword("");
        setError(err.detail || "Incorrect password. Please try again.");
        return;
      }

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Server error" }));
        throw new Error(err.detail || "Backend error");
      }

      const result = await res.json();

      // PDF is encrypted — backend needs password
      if (result.status === "encrypted") {
        setNeedsPassword(true);
        setPendingFile(file);
        setError(null);
        return;
      }

      if (result.status === "error") throw new Error(result.message || "Extraction failed");
      if (!result.data?.length) {
        setError("No transactions found in this statement.");
      } else {
        setTransactions(result.data);
        setPendingFile(null);
        setPdfPassword("");
      }
    } catch (err: any) {
      setError(err.message || "Connection failed. Please check the server.");
    } finally {
      setLoading(false);
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setFileName(file.name);
    setPendingFile(file);
    setPdfPassword("");
    setNeedsPassword(false);
    await extractFromFile(file);
  };

  const handlePasswordSubmit = async () => {
    if (!pendingFile || !pdfPassword.trim()) return;
    await extractFromFile(pendingFile, pdfPassword.trim());
  };


  const formatTallyDate = (d: string) => {
    const p = d.split(/[-/.]/);
    if (p.length !== 3) return d.replace(/\D/g, "").slice(0, 8);
    let [a, b, c] = p.map(s => s.trim());
    if (a.length === 4) return a + b.padStart(2,"0") + c.padStart(2,"0");
    if (c.length === 2) c = "20" + c;
    return c + b.padStart(2,"0") + a.padStart(2,"0");
  };

  const downloadXML = () => {
    if (!transactions.length) return;
    const guid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });

    let xml = `<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA><REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <LEDGER NAME="${bankLedger}" ACTION="Create"><NAME.LIST><NAME>${bankLedger}</NAME></NAME.LIST><PARENT>Bank Accounts</PARENT><ISBILLWISEON>No</ISBILLWISEON></LEDGER>
  <LEDGER NAME="${suspenseLedger}" ACTION="Create"><NAME.LIST><NAME>${suspenseLedger}</NAME></NAME.LIST><PARENT>Suspense Accounts</PARENT><ISBILLWISEON>No</ISBILLWISEON></LEDGER>`;

    transactions.forEach(txn => {
      const debit = parseFloat(txn.debit) || 0;
      const credit = parseFloat(txn.credit) || 0;
      const isReceipt = credit > 0;
      const amount = isReceipt ? credit : debit;
      if (!amount) return;
      const type = isReceipt ? "Receipt" : "Payment";
      xml += `
  <VOUCHER VCHTYPE="${type}" ACTION="Create">
    <DATE>${formatTallyDate(txn.date)}</DATE><GUID>${guid()}</GUID>
    <VOUCHERTYPENAME>${type}</VOUCHERTYPENAME><NARRATION>${txn.narration}</NARRATION>
    <ALLLEDGERENTRIES.LIST><LEDGERNAME>${bankLedger}</LEDGERNAME><ISDEEMEDPOSITIVE>${isReceipt?"YES":"NO"}</ISDEEMEDPOSITIVE><AMOUNT>${isReceipt?"-"+amount.toFixed(2):amount.toFixed(2)}</AMOUNT></ALLLEDGERENTRIES.LIST>
    <ALLLEDGERENTRIES.LIST><LEDGERNAME>${suspenseLedger}</LEDGERNAME><ISDEEMEDPOSITIVE>${isReceipt?"NO":"YES"}</ISDEEMEDPOSITIVE><AMOUNT>${isReceipt?amount.toFixed(2):"-"+amount.toFixed(2)}</AMOUNT></ALLLEDGERENTRIES.LIST>
  </VOUCHER>`;
    });
    xml += `\n</TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>`;
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(new Blob([xml], { type: "text/xml" })),
      download: `makhantaxwell_${Date.now()}.xml`,
    });
    a.click();
  };

  return (
    <main className="min-h-screen bg-white">
      <Navbar />

      <div className="max-w-4xl mx-auto px-6">
        {/* ── Hero ── */}
        <section className="pt-40 pb-20 text-center">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6 }}
          >
            <span className="inline-block px-4 py-1.5 rounded-full bg-blue-50 text-blue-600 text-xs font-bold uppercase tracking-widest mb-6">
              Automated Tally Integration
            </span>
            <h1 className="text-4xl md:text-6xl font-extrabold text-slate-900 mb-6 leading-tight">
              Convert Bank PDF to <br />
              <span className="text-blue-600">Tally XML</span> instantly
            </h1>
            <p className="text-lg text-slate-500 mb-10 max-w-2xl mx-auto font-medium">
              Eliminate hours of manual data entry. Upload any bank statement and get a perfectly formatted Tally XML file in seconds.
            </p>
            <div className="flex flex-col sm:flex-row justify-center gap-4">
              <button 
                onClick={scrollToConverter}
                className="bg-blue-600 text-white px-8 py-4 rounded-xl font-bold text-lg hover:bg-blue-700 transition-all shadow-xl shadow-blue-100 active:scale-95"
              >
                Get Started
              </button>
              <Link 
                href="/download"
                className="bg-white text-slate-700 border border-slate-200 px-8 py-4 rounded-xl font-bold text-lg hover:bg-slate-50 transition-all active:scale-95"
              >
                Download TCP
              </Link>
            </div>
          </motion.div>
        </section>

        {/* ── Converter ── */}
        <section id="converter" ref={converterRef} className="py-20 scroll-mt-24">
          <div className="bg-white rounded-3xl border border-slate-200 shadow-xl overflow-hidden">
            <div className="p-8 md:p-12">
              <div className="flex items-center gap-4 mb-8">
                <div className="w-10 h-10 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold text-lg">1</div>
                <h2 className="text-2xl font-bold text-slate-900">Upload PDF</h2>
              </div>

              <label className={`relative flex flex-col items-center justify-center gap-6 p-12 md:p-20 rounded-2xl border-2 border-dashed cursor-pointer transition-all
                ${loading ? "bg-slate-50 border-slate-200 cursor-wait" : "bg-blue-50/20 border-blue-100 hover:bg-blue-50/50 hover:border-blue-300"}`}>
                <input type="file" accept="application/pdf" onChange={handleFileUpload} className="hidden" disabled={loading} />
                <div className="w-16 h-16 rounded-2xl bg-white shadow-lg flex items-center justify-center text-blue-600">
                  {loading ? <Database className="animate-spin" size={32} /> : <Upload size={32} />}
                </div>
                <div className="text-center">
                  <p className="text-xl font-bold text-slate-900 mb-1">{loading ? "Processing..." : fileName ? fileName : "Choose Bank Statement"}</p>
                  <p className="text-slate-400 font-medium">{loading ? (useAI ? "Gemini 2.5 Flash is thinking..." : "Identifying transactions...") : "or drag and drop your PDF here"}</p>
                </div>
              </label>

              {/* ── AI Toggle ── */}
              <div className="mt-6 flex items-center justify-between p-4 bg-blue-50/30 rounded-2xl border border-blue-100">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-xl bg-blue-600 flex items-center justify-center text-white">
                    <Database size={20} className={useAI ? "animate-pulse" : ""} />
                  </div>
                  <div>
                    <p className="font-bold text-slate-900 text-sm">AI Powered Engine (Gemini)</p>
                    <p className="text-slate-500 text-xs font-medium">Use Gemini 2.5 Flash for complex statement layouts</p>
                  </div>
                </div>
                <button 
                  onClick={() => setUseAI(!useAI)}
                  className={`relative w-14 h-8 rounded-full transition-all duration-300 ${useAI ? "bg-blue-600" : "bg-slate-200"}`}
                >
                  <div className={`absolute top-1 left-1 w-6 h-6 bg-white rounded-full transition-all duration-300 shadow-sm ${useAI ? "translate-x-6" : ""}`} />
                </button>
              </div>

              <AnimatePresence>
                {needsPassword && (
                  <motion.div 
                    initial={{ opacity: 0, y: -10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                    className="mt-6 p-6 bg-amber-50 border border-amber-100 rounded-2xl"
                  >
                    <div className="flex items-center gap-3 mb-4 text-amber-900 font-bold">
                      <Lock size={20} className="text-amber-600" />
                      <span>Password Required</span>
                    </div>
                    <div className="flex flex-col sm:flex-row gap-3">
                      <div className="relative flex-1">
                        <KeyRound className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400" size={18} />
                        <input 
                          type="password" 
                          placeholder="Enter PDF Password"
                          value={pdfPassword}
                          onChange={(e) => setPdfPassword(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && handlePasswordSubmit()}
                          className="w-full bg-white border border-slate-200 rounded-xl pl-12 pr-4 py-3 font-bold text-slate-900 outline-none focus:ring-2 focus:ring-blue-500"
                        />
                      </div>
                      <button 
                        onClick={handlePasswordSubmit}
                        disabled={loading || !pdfPassword.trim()}
                        className="bg-slate-900 text-white px-8 py-3 rounded-xl font-bold hover:bg-blue-600 disabled:bg-slate-300 transition-all active:scale-95 shadow-lg shadow-slate-200"
                      >
                        {loading ? "Decrypting..." : "Unlock & Extract"}
                      </button>
                    </div>
                    <p className="mt-3 text-amber-700/70 text-xs font-semibold px-1">
                      Bank statements are often protected by your DOB or Account Number.
                    </p>
                  </motion.div>
                )}
              </AnimatePresence>

              <AnimatePresence>
                {error && (
                  <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="mt-6 flex items-center gap-3 bg-red-50 text-red-600 p-4 rounded-xl border border-red-100 font-bold">
                    <AlertCircle size={20} />
                    <span>{error}</span>
                  </motion.div>
                )}
              </AnimatePresence>

              {transactions.length > 0 && (
                <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="mt-12">
                  <div className="bg-emerald-50 border border-emerald-100 rounded-2xl p-6 mb-10 flex flex-col md:flex-row items-center justify-between gap-6">
                    <div className="flex items-center gap-4 text-center md:text-left">
                      <div className="w-12 h-12 rounded-full bg-emerald-500 flex items-center justify-center text-white shrink-0">
                        <CheckCircle2 size={24} />
                      </div>
                      <div>
                        <p className="font-bold text-emerald-900 text-lg">{transactions.length} Transactions</p>
                        <p className="text-emerald-600 font-medium text-sm">Successfully extracted and ready.</p>
                      </div>
                    </div>
                    <button onClick={downloadXML} className="w-full md:w-auto bg-slate-900 text-white px-8 py-3 rounded-xl font-bold hover:bg-blue-600 transition-all shadow-lg">
                      Download XML
                    </button>
                  </div>

                  <div className="space-y-10">
                    <div>
                      <div className="flex items-center gap-4 mb-6">
                        <div className="w-10 h-10 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold text-lg">2</div>
                        <h3 className="text-xl font-bold text-slate-900">Configure Ledgers</h3>
                      </div>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                        <div className="space-y-2">
                          <label className="text-xs font-bold text-slate-400 uppercase tracking-wider ml-1">Bank Ledger</label>
                          <input type="text" value={bankLedger} onChange={e => setBankLedger(e.target.value)} className="w-full bg-slate-50 border border-slate-200 rounded-xl px-5 py-3.5 font-bold text-slate-900 focus:ring-2 focus:ring-blue-500 outline-none" />
                        </div>
                        <div className="space-y-2">
                          <label className="text-xs font-bold text-slate-400 uppercase tracking-wider ml-1">Suspense Ledger</label>
                          <input type="text" value={suspenseLedger} onChange={e => setSuspenseLedger(e.target.value)} className="w-full bg-slate-50 border border-slate-200 rounded-xl px-5 py-3.5 font-bold text-slate-900 focus:ring-2 focus:ring-blue-500 outline-none" />
                        </div>
                      </div>
                    </div>

                    <div>
                      <div className="flex items-center gap-4 mb-6">
                        <div className="w-10 h-10 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold text-lg">3</div>
                        <h3 className="text-xl font-bold text-slate-900">Preview Data</h3>
                      </div>
                      <div className="border border-slate-100 rounded-2xl overflow-hidden bg-slate-50/30">
                        <div className="max-h-[400px] overflow-y-auto">
                          <table className="w-full text-sm">
                            <thead className="bg-slate-50 border-b border-slate-100 sticky top-0">
                              <tr className="text-slate-400 font-bold uppercase text-[10px] tracking-widest">
                                <th className="px-6 py-4 text-left">Date</th>
                                <th className="px-6 py-4 text-left">Narration</th>
                                <th className="px-6 py-4 text-right">Debit</th>
                                <th className="px-6 py-4 text-right">Credit</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-50">
                              {transactions.map((txn, i) => (
                                <tr key={i} className="hover:bg-blue-50 transition-colors bg-white">
                                  <td className="px-6 py-4 font-bold text-slate-900 whitespace-nowrap">{txn.date}</td>
                                  <td className="px-6 py-4 text-slate-500 font-medium truncate max-w-[200px]">{txn.narration}</td>
                                  <td className="px-6 py-4 text-right font-bold text-red-600">{txn.debit > 0 ? `₹${parseFloat(txn.debit).toLocaleString()}` : "—"}</td>
                                  <td className="px-6 py-4 text-right font-bold text-emerald-600">{txn.credit > 0 ? `₹${parseFloat(txn.credit).toLocaleString()}` : "—"}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    </div>
                  </div>
                </motion.div>
              )}
            </div>
            <div className="bg-slate-900 p-8 flex items-center justify-between">
              <div className="flex gap-6">
                <div className="flex items-center gap-2 text-[10px] font-bold text-slate-500 tracking-tighter uppercase">
                  <div className="w-1.5 h-1.5 rounded-full bg-emerald-500" /> Secure SSL
                </div>
                <div className="flex items-center gap-2 text-[10px] font-bold text-slate-500 tracking-tighter uppercase">
                  <div className="w-1.5 h-1.5 rounded-full bg-blue-500" /> Data Encrypted
                </div>
              </div>
              <FileSpreadsheet size={20} className="text-slate-700" />
            </div>
          </div>
        </section>
      </div>

      <Footer />
    </main>
  );
}