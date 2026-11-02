'use client';

import { useEffect, useRef, useState } from 'react';
import './store.css';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

const ICONS = [
  ['headphone', '🎧', '#ffe3ec'],
  ['keyboard', '⌨️', '#e4ebff'],
  ['hub', '🔌', '#dcf5ec'],
  ['stand', '💻', '#fff1d6'],
  ['monitor', '🖥️', '#ece4ff'],
];
const look = (name) => {
  const hit = ICONS.find(([k]) => name.toLowerCase().includes(k));
  return hit ? { icon: hit[1], bg: hit[2] } : { icon: '🛍️', bg: '#eef1f7' };
};

const HINTS = [
  'I need a keyboard for my desk',
  'Something to connect my laptop',
  'What monitors do you have?',
];

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

// Sent with every message so a retried request reuses the same PayPal checkout
// instead of creating a second one.
const newRequestId = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2);

export default function Home() {
  const [messages, setMessages] = useState([
    { role: 'assistant', content: "Hi, I'm PayAgent. Tell me what you need and I'll find it and set up the payment." },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [checkout, setCheckout] = useState(null);
  const [products, setProducts] = useState([]);
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
  }, []);

  useEffect(() => {
    fetch(`${API}/products`).then((r) => r.json()).then(setProducts).catch(() => {});
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, loading, checkout]);

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

  // The store owner reviews the evidence outside the chat, so the request
  // status is polled here instead of being decided by the assistant.
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
      if (data.action === 'init_paypal_checkout' && data.checkout?.approval_url) {
        setCheckout(data.checkout);
      }
      if (data.approval) setPending(data.approval);
      if (data.refund) setRefund({ ...data.refund, uploaded: 0 });
    } catch (err) {
      console.error(err);
      setMessages((p) => [
        ...p,
        { role: 'assistant', content: "I couldn't reach the store service. Check that the API is running and try again." },
      ]);
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
      setRefund((prev) => ({
        ...prev,
        status: data.status,
        uploaded: (prev?.uploaded || 0) + 1,
      }));
    } catch (err) {
      setRefundError(err.message);
    } finally {
      setUploading(false);
    }
  };

  return (
    <main className="shell">
      <header className="top">
        <div className="brand">
          <img className="mark" src="/logo.png" alt="PayAgent" />
          <div>
            <h1>PayAgent</h1>
            <p>Shop by talking. Pay with PayPal.</p>
          </div>
        </div>
        <span className="sandbox">PayPal Sandbox: no real money</span>
      </header>

      <div className="grid">
        <section className="panel chat" aria-label="Chat with PayAgent">
          <div className="feed">
            {messages.map((m, i) => (
              <div key={i} className={`row ${m.role}`}>
                <div className="bubble">{m.content}</div>
              </div>
            ))}
            {loading && (
              <div className="row assistant">
                <div className="typing" aria-label="PayAgent is typing"><i /><i /><i /></div>
              </div>
            )}
            {pending && (
              <div className="wait">
                Waiting for the store owner to approve ${pending.total_usd.toFixed(2)}. This page updates by itself.
              </div>
            )}
            {refund && (
              <div className="evidence">
                <div className="row2" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                  <b>Refund request · ${Number(refund.amount_usd || 0).toFixed(2)}</b>
                  <span className={`chip ${refund.status === 'REFUNDED' ? 'ok' : CLOSED_REFUNDS.includes(refund.status) ? 'bad' : 'wait'}`}>
                    {REFUND_LABEL[refund.status] || refund.status}
                  </span>
                </div>
                {!CLOSED_REFUNDS.includes(refund.status) && (
                  <>
                    <p className="note">
                      Attach photos or a short video (up to 15 MB, 60 seconds) of the problem.
                      The store owner reviews them.
                    </p>
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
                <div>
                  <b>${checkout.total_usd.toFixed(2)} USD</b>
                  <span>Your order is ready to pay</span>
                </div>
                <a className="paybtn" href={checkout.approval_url}>Pay with PayPal</a>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {messages.length < 2 && (
            <div className="hints">
              {HINTS.map((h) => (
                <button key={h} className="hint" onClick={() => send(h)}>{h}</button>
              ))}
            </div>
          )}

          <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Tell me what you need"
              aria-label="Message"
            />
            <button className="send" type="submit" disabled={loading || !input.trim()}>Send</button>
          </form>
        </section>

        <aside className="panel shelf">
          <h2>In stock</h2>
          <p>Tap an item and PayAgent takes it from there.</p>
          <div className="items">
            {products.map((p) => {
              const { icon, bg } = look(p.name);
              return (
                <button key={p.product_id} className="item" disabled={loading} onClick={() => send(`I want the ${p.name}`)}>
                  <span className="thumb" style={{ background: bg }}>{icon}</span>
                  <span>
                    <strong>{p.name}</strong>
                    <small>Ask PayAgent to buy</small>
                  </span>
                  <span className="price">${p.price_usd.toFixed(2)}</span>
                </button>
              );
            })}
          </div>
          <div className="trust">
            Prices come from the store, never from the AI. Orders above the safety limit need a person to approve them.
          </div>
        </aside>
      </div>
    </main>
  );
}
