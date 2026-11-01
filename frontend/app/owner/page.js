'use client';

import { useEffect, useState } from 'react';
import '../store.css';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export default function Owner() {
  const [token, setToken] = useState('');
  const [items, setItems] = useState([]);
  const [orders, setOrders] = useState([]);
  const [error, setError] = useState('');

  const load = async (t = token) => {
    if (!t) return;
    try {
      const res = await fetch(`${API}/owner/approvals`, { headers: { 'x-admin-token': t } });
      if (!res.ok) throw new Error(res.status === 401 ? 'Wrong owner token' : 'Request failed');
      setItems(await res.json());
      const r2 = await fetch(`${API}/owner/orders`, { headers: { 'x-admin-token': t } });
      if (r2.ok) setOrders(await r2.json());
      setError('');
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    if (!token) return;
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [token]);

  const decide = async (id, approve) => {
    const res = await fetch(`${API}/owner/approvals/${id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-admin-token': token },
      body: JSON.stringify({ approve }),
    });
    if (!res.ok) setError('Could not complete this action');
    load();
  };

  return (
    <main className="owner">
      <h1>Owner panel</h1>
      <p>Approve or decline what PayAgent cannot decide alone.</p>
      <div className="tokenrow">
        <input
          type="password"
          placeholder="Owner token"
          onChange={(e) => setToken(e.target.value)}
          aria-label="Owner token"
        />
      </div>
      {error && <p style={{ color: '#a12020' }}>{error}</p>}
      {token && !error && items.length === 0 && <div className="empty">Nothing waiting for approval.</div>}
      {items.map((r) => (
        <div className="req" key={r.id}>
          <div className="tag">{r.kind === 'refund' ? 'Refund' : 'Large purchase'}</div>
          <b>${r.total_usd.toFixed(2)} USD</b>
          <span>{r.summary}</span>
          <div className="btns">
            <button className="yes" onClick={() => decide(r.id, true)}>Approve</button>
            <button className="no" onClick={() => decide(r.id, false)}>Decline</button>
          </div>
        </div>
      ))}
      {orders.length > 0 && <h2 style={{ fontFamily: 'var(--display)', margin: '26px 0 12px' }}>Recent orders</h2>}
      {orders.map((o) => (
        <div className="req" key={o.order_id}>
          <div className="tag">{o.status}</div>
          <b>${o.total_usd.toFixed(2)} USD</b>
          <span>{o.item}</span>
        </div>
      ))}
    </main>
  );
}
