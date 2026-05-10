"use client";

import { motion } from "framer-motion";
import { Download, FileCode, ArrowLeft } from "lucide-react";
import Link from "next/link";

const Navbar = () => (
  <nav className="fixed top-0 left-0 right-0 z-50 bg-white border-b border-slate-200">
    <div className="max-w-5xl mx-auto px-6 md:px-10 h-20 flex justify-between items-center">
      <Link href="/" className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-blue-600 flex items-center justify-center">
          <span className="text-white font-black text-xl">M.T</span>
        </div>
        <span className="font-extrabold text-2xl tracking-tight text-slate-900">
          makhantaxwell<span className="text-blue-600">.</span>
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

const Footer = () => (
  <footer className="bg-white border-t border-slate-100 py-12">
    <div className="max-w-5xl mx-auto px-6 md:px-10 text-center">
      <div className="flex items-center justify-center gap-2 mb-4">
        <div className="w-6 h-6 rounded bg-slate-900 flex items-center justify-center text-[10px] text-white font-bold">M.T</div>
        <span className="font-bold text-slate-900">makhantaxwell.</span>
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

export default function DownloadPage() {
  return (
    <main className="min-h-screen bg-white">
      <Navbar />
      
      <div className="max-w-4xl mx-auto px-6">
        <section className="pt-40 pb-24 text-center">
          <motion.div 
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="mb-16"
          >
            <Link href="/" className="inline-flex items-center gap-2 text-blue-600 font-bold mb-10 hover:gap-3 transition-all bg-white px-6 py-2.5 rounded-xl border border-slate-100 shadow-sm">
              <ArrowLeft size={18} /> Back to Home
            </Link>
            <h1 className="text-4xl md:text-5xl font-extrabold text-slate-900 mb-6 tracking-tight">Configuration Files</h1>
            <p className="text-lg text-slate-500 max-w-xl mx-auto font-medium">Download the required TCP files to enable advanced automation in your Tally environment.</p>
          </motion.div>

          <div className="max-w-2xl mx-auto mb-20">
            <motion.div 
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: 0.1 }}
              className="bg-white p-8 md:p-12 rounded-3xl border border-slate-200 shadow-xl flex flex-col md:flex-row items-center gap-8"
            >
              <div className="w-16 h-16 rounded-2xl bg-blue-50 text-blue-600 flex items-center justify-center shrink-0">
                <FileCode size={32} />
              </div>
              <div className="flex-1 text-center md:text-left">
                <h3 className="text-2xl font-bold text-slate-900 mb-1">makhantaxwell.tcp</h3>
                <p className="text-slate-400 font-bold text-xs uppercase tracking-widest">Core Engine</p>
              </div>
              <a 
                href="/makhantaxwell.tcp" 
                download="makhantaxwell.tcp"
                className="w-full md:w-auto flex items-center justify-center gap-3 bg-blue-600 hover:bg-blue-700 text-white font-bold px-10 py-4 rounded-xl transition-all shadow-xl shadow-blue-100 active:scale-95 text-lg"
              >
                <Download size={22} /> Download
              </a>
            </motion.div>
            
            <div className="mt-12 p-8 rounded-2xl bg-slate-50 border border-slate-100 text-left">
              <h4 className="text-lg font-bold text-slate-900 mb-4">Quick Setup Guide</h4>
              <ol className="text-slate-600 space-y-3 list-decimal ml-5 font-medium">
                <li>Download the <code className="bg-white px-1.5 py-0.5 rounded border border-slate-200 font-mono text-blue-600">.tcp</code> file.</li>
                <li>Place it in your Tally installation folder.</li>
                <li>Go to <span className="font-bold">F1 {'>'} TDLs & Add-ons</span> and load the file.</li>
                <li>Restart Tally to see the new automated features.</li>
              </ol>
            </div>
          </div>
        </section>
      </div>

      <Footer />
    </main>
  );
}
