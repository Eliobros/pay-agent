'use client';

import { useEffect, useRef, useState } from 'react';
import '../store.css';

const API = '/api/backend';

const REFUND_LABEL = {
  REQUESTED: 'Request sent',
  WAITING_EVIDENCE: 'Waiting for your photos',
  UNDER_REVIEW: 'The store owner is reviewing your evidence',
  MORE_EVIDENCE_REQUIRED: 'More evidence needed',
  APPROVED: 'Approved',
  PROCESSING: 'Refund in progress',
  REFUNDED: 'Refunded',
  REJECTED: 'Rejected',
  FAILED: 'The refund failed',
};

const CLOSED_REFUNDS = ['REFUNDED', 'REJECTED', 'FAILED'];

// Sent with every message so a retried request reuses the same PayPal checkout.
const newRequestId = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2);

export default function Embed() {
  const [store, setStore] = useState('PayAgent');
  const [messages, setMessages] = useState([
    { role: 'assistant', content: 'Hi! Tell me what you are looking for and I will set up the payment.' },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [checkout, setCheckout] = useState(null);
  const [pending, setPending] = useState(null);
  const [refund, setRefund] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [refundError, setRefundError] = useState('');
  const [visitor, setVisitor] = useState('');
  const endRef = useRef(null);

  useEffect(() => {
    let id = null;
    try { id = localStorage.getItem('payagent_visitor'); } catch {}
    if (!id) {
      id = (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : Date.now().toString(36) + Math.random().toString(36).slice(2);
      try { localStorage.setItem('payagent_visitor', id); } catch {}
    }
    setVisitor(id);
    fetch(`${API}/config`).then((r) => r.json()).then((c) => c.store_name && setStore(c.store_name)).catch(() => {});
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, loading, checkout, pending]);

  useEffect(() => {
    if (!pending) return;
    const t = setInterval(async () => {
      try {
        const d = await (await fetch(`${API}/approvals/${pending.approval_id}`)).json();
        if (d.status === 'approved') {
          setPending(null);
          if (d.checkout) setCheckout(d.checkout);
          setMessages((p) => [...p, {
            role: 'assistant',
            content: d.kind === 'refund' ? 'The store owner approved your refund.' : 'Approved! Your order is ready to pay.',
          }]);
        } else if (d.status === 'rejected') {
          setPending(null);
          setMessages((p) => [...p, { role: 'assistant', content: 'The store owner declined this request.' }]);
        }
      } catch {}
    }, 3000);
    return () => clearInterval(t);
  }, [pending]);

  useEffect(() => {
    if (!refund?.request_id || !visitor) return;
    const t = setInterval(async () => {
      try {
        const res = await fetch(
          `${API}/refunds/${refund.request_id}?user_id=${encodeURIComponent(visitor)}`,
        );
        if (!res.ok) return;
        const d = await res.json();
        setRefund((prev) => (prev ? { ...prev, status: d.status, amount_usd: d.amount_usd } : prev));
      } catch {}
    }, 5000);
    return () => clearInterval(t);
  }, [refund?.request_id, visitor]);

  const send = async (text) => {
    const msg = text.trim();
    if (!msg || loading) return;
    setInput('');
    setMessages((p) => [...p, { role: 'user', content: msg }]);
    setLoading(true);
    setCheckout(null);
    try {
      const res = await fetch(`${API}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, user_id: visitor || 'guest', request_id: newRequestId() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Request failed');
      setMessages((p) => [...p, { role: 'assistant', content: data.response }]);
      if (data.action === 'init_paypal_checkout' && data.checkout?.approval_url) setCheckout(data.checkout);
      if (data.approval) setPending(data.approval);
      if (data.refund) setRefund({ ...data.refund, uploaded: 0 });
    } catch (err) {
      const text = String(err.message || '').startsWith('Too many') ? err.message : "I couldn't reach the store service. Please try again.";
      setMessages((p) => [...p, { role: 'assistant', content: text }]);
    } finally {
      setLoading(false);
    }
  };

  const uploadEvidence = async (file) => {
    if (!refund?.request_id || !file) return;
    setUploading(true);
    setRefundError('');
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch(
        `${API}/refunds/${refund.request_id}/evidence?user_id=${encodeURIComponent(visitor)}`,
        { method: 'POST', body: form },
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not upload this file');
      setRefund((prev) => ({ ...prev, status: data.status, uploaded: (prev?.uploaded || 0) + 1 }));
    } catch (err) {
      setRefundError(err.message);
    } finally {
      setUploading(false);
    }
  };

  const closeWidget = () => window.parent?.postMessage({ payagent: 'close' }, '*');

  return (
    <section className="chat" style={{ height: '100vh', borderRadius: 0, boxShadow: 'none', background: '#fff' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 16px', borderBottom: '1px solid var(--line)' }}>
        <strong style={{ fontFamily: 'var(--display)', fontSize: 18 }}>{store}</strong>
        <button onClick={closeWidget} aria-label="Close chat" style={{ border: 0, background: '#eef1f7', borderRadius: 999, width: 34, height: 34, fontSize: 16, cursor: 'pointer' }}>✕</button>
      </div>

      <div className="feed">
        {messages.map((m, i) => (
          <div key={i} className={`row ${m.role}`}><div className="bubble">{m.content}</div></div>
        ))}
        {loading && <div className="row assistant"><div className="typing"><i /><i /><i /></div></div>}
        {pending && (
          <div className="wait">Waiting for the store owner to approve ${pending.total_usd.toFixed(2)}. This updates by itself.</div>
        )}
        {refund && (
          <div className="evidence">
            <div className="row2" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <b>Refund · ${Number(refund.amount_usd || 0).toFixed(2)}</b>
              <span className={`chip ${refund.status === 'REFUNDED' ? 'ok' : CLOSED_REFUNDS.includes(refund.status) ? 'bad' : 'wait'}`}>
                {REFUND_LABEL[refund.status] || refund.status}
              </span>
            </div>
            {!CLOSED_REFUNDS.includes(refund.status) && (
              <>
                <p className="note">Attach photos or a short video (up to 15 MB, 60 seconds) of the problem.</p>
                <input
                  type="file"
                  accept="image/jpeg,image/png,image/webp,video/mp4,video/webm"
                  disabled={uploading}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) uploadEvidence(file);
                    e.target.value = '';
                  }}
                />
              </>
            )}
            {refund.uploaded > 0 && <p className="note">{refund.uploaded} file(s) sent.</p>}
            {refundError && <p className="note" style={{ color: '#a12020' }}>{refundError}</p>}
          </div>
        )}
        {checkout && (
          <div className="pay">
            <div><b>${checkout.total_usd.toFixed(2)} USD</b><span>Your order is ready to pay</span></div>
            <a className="paybtn" href={checkout.approval_url} target="_blank" rel="noopener noreferrer">Pay with PayPal</a>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Tell me what you need" aria-label="Message" />
        <button className="send" type="submit" disabled={loading || !input.trim()}>Send</button>
      </form>
    </section>
  );
}
