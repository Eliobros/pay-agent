'use client';

import { useEffect, useState } from 'react';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export default function Success() {
  const [state, setState] = useState({ status: 'loading', message: 'Confirming your payment...' });

  useEffect(() => {
    // PayPal redirects here with ?token=<order_id>&PayerID=...
    const orderId = new URLSearchParams(window.location.search).get('token');
    if (!orderId) {
      setState({ status: 'error', message: 'Missing order information.' });
      return;
    }
    fetch(`${API}/paypal/capture/${orderId}`, { method: 'POST' })
      .then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Capture failed');
        setState({
          status: data.status === 'COMPLETED' ? 'ok' : 'error',
          message:
            data.status === 'COMPLETED'
              ? `Payment completed! Order ${data.order_id}`
              : `Payment status: ${data.status}`,
        });
      })
      .catch((err) => setState({ status: 'error', message: err.message }));
  }, []);

  const color = state.status === 'ok' ? '#155724' : state.status === 'error' ? '#721c24' : '#333';

  return (
    <main style={{ maxWidth: 600, margin: '80px auto', fontFamily: 'sans-serif', textAlign: 'center', padding: 20 }}>
      <h1>{state.status === 'ok' ? '✅' : state.status === 'error' ? '⚠️' : '⏳'} PayAgent</h1>
      <p style={{ color, fontWeight: 'bold' }}>{state.message}</p>
      <a href="/" style={{ color: '#0070ba' }}>Back to the store</a>
    </main>
  );
}
