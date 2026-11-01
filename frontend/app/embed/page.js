'use client';

import { useEffect, useRef, useState } from 'react';
import '../store.css';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export default function Embed() {
  const [store, setStore] = useState('PayAgent');
  const [messages, setMessages] = useState([
    { role: 'assistant', content: 'Hi! Tell me what you are looking for and I will set up the payment.' },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [checkout, setCheckout] = useState(null);
  const [pending, setPending] = useState(null);
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
        body: JSON.stringify({ message: msg, user_id: visitor || 'guest' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Request failed');
      setMessages((p) => [...p, { role: 'assistant', content: data.response }]);
      if (data.action === 'init_paypal_checkout' && data.checkout?.approval_url) setCheckout(data.checkout);
      if (data.approval) setPending(data.approval);
    } catch (err) {
      const text = String(err.message || '').startsWith('Too many') ? err.message : "I couldn't reach the store service. Please try again.";
      setMessages((p) => [...p, { role: 'assistant', content: text }]);
    } finally {
      setLoading(false);
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
