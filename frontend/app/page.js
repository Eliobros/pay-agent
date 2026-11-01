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

export default function Home() {
  const [messages, setMessages] = useState([
    { role: 'assistant', content: "Hi, I'm PayAgent. Tell me what you need and I'll find it and set up the payment." },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [checkout, setCheckout] = useState(null);
  const [products, setProducts] = useState([]);
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
      if (data.action === 'init_paypal_checkout' && data.checkout?.approval_url) {
        setCheckout(data.checkout);
      }
      if (data.approval) setPending(data.approval);
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

  return (
    <main className="shell">
      <header className="top">
        <div className="brand">
          <div className="mark">🛍️</div>
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
