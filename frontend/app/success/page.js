'use client';

import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

import { GREETING, PAID_MESSAGE, loadChatSession, saveChatSession } from '../chatSession';

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

const readSession = () => loadChatSession() || [{ role: 'assistant', content: GREETING }];

function Screen({ status, message }) {
  const color = status === 'ok' ? '#155724' : status === 'error' ? '#721c24' : '#333';
  return (
    <main style={{ maxWidth: 600, margin: '80px auto', fontFamily: 'sans-serif', textAlign: 'center', padding: 20 }}>
      <h1>{status === 'ok' ? '✅' : status === 'error' ? '⚠️' : '⏳'} PayAgent</h1>
      <p style={{ color, fontWeight: 'bold' }}>{message}</p>
      <a href="/" style={{ color: '#0070ba' }}>Back to the store</a>
    </main>
  );
}

function ConfirmPayment() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get('token');
  const payerId = searchParams.get('PayerID');
  const [state, setState] = useState({ status: 'loading', message: 'Confirming your payment...' });

  useEffect(() => {
    // PayPal redirects here with ?token=<order_id>&PayerID=...
    if (!token) {
      setState({ status: 'error', message: 'Missing order information.' });
      return;
    }
    let cancelled = false;

    fetch(`${API}/paypal/capture/${encodeURIComponent(token)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payer_id: payerId || '' }),
    })
      .then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Capture failed');
        if (data.status !== 'COMPLETED') throw new Error(`Payment status: ${data.status}`);
        if (cancelled) return;

        // Write the confirmation into the saved conversation so the chat page
        // shows it as soon as it loads the session back. The pending order is
        // left in place: the chat clears it once it sees this message.
        const session = readSession();
        if (session[session.length - 1]?.content !== PAID_MESSAGE) {
          session.push({ role: 'assistant', content: PAID_MESSAGE });
          saveChatSession(session);
        }
        setState({ status: 'ok', message: 'Payment completed!' });
        router.push('/');
      })
      .catch((err) => {
        if (!cancelled) setState({ status: 'error', message: err.message });
      });

    return () => { cancelled = true; };
  }, [token, payerId, router]);

  return <Screen status={state.status} message={state.message} />;
}

export default function Success() {
  return (
    <Suspense fallback={<Screen status="loading" message="Confirming your payment..." />}>
      <ConfirmPayment />
    </Suspense>
  );
}
