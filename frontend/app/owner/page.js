'use client';

import { useCallback, useEffect, useState } from 'react';
import '../store.css';

// Same-origin path proxied to the API by next.config.js, so the session
// cookie belongs to this site and the browser always sends it back.
const API = '/api/backend';

// The session lives in an HTTP-only cookie, so every request must be sent
// with credentials and no token is ever kept in the browser.
const call = (path, options = {}) =>
  fetch(`${API}${path}`, { credentials: 'include', ...options });

const json = (path, body) =>
  call(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

const patch = (path, body) =>
  call(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

const money = (value) => `$${Number(value || 0).toFixed(2)}`;
const when = (iso) => (iso ? iso.slice(0, 16).replace('T', ' ') : '—');

const TABS = [
  ['overview', 'Overview'],
  ['orders', 'Orders'],
  ['refunds', 'Refund requests'],
  ['providers', 'Payment providers'],
  ['usage', 'AI usage'],
  ['settings', 'Store settings'],
  ['security', 'Security & account'],
];

const STATUS_LABEL = {
  operational: 'Operational',
  invalid: 'Invalid credentials',
  unverified: 'Not validated',
};

const REFUND_CLOSED = ['REFUNDED', 'REJECTED', 'FAILED'];

const refundTone = (status) => {
  if (status === 'REFUNDED') return 'ok';
  if (['REJECTED', 'FAILED'].includes(status)) return 'bad';
  return 'wait';
};

// Evidence is fetched with the owner cookie and shown from a blob URL, so no
// public link to a private file exists anywhere in the page.
function Evidence({ requestId, item }) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);

  const load = async () => {
    const res = await call(`/owner/refund-requests/${requestId}/evidence/${item.id}`);
    if (!res.ok) {
      setError(res.status === 410 ? 'This file was deleted by the retention policy' : 'Could not load this file');
      return;
    }
    setUrl(URL.createObjectURL(await res.blob()));
  };

  if (error) return <p className="hint" style={{ color: '#a12020' }}>{error}</p>;
  if (url) {
    return item.kind === 'video' ? (
      <video src={url} controls preload="metadata" />
    ) : (
      <img src={url} alt="Refund evidence" />
    );
  }
  return (
    <button type="button" className="hint" onClick={load}>
      {item.kind === 'video' ? '▶ Load video' : 'Load photo'} · {Math.round(item.size / 1024)} KB
    </button>
  );
}

function OverviewSection({ overview, approvals, onDecide }) {
  if (!overview) return <div className="empty">Loading…</div>;
  return (
    <>
    <div className="stats">
      <div className="stat"><b>{overview.orders_total}</b><span>Total orders</span></div>
      <div className="stat good"><b>{overview.orders_paid}</b><span>Successful payments</span></div>
      <div className="stat"><b>{money(overview.total_sales_usd)}</b><span>Total sales</span></div>
      <div className="stat"><b>{money(overview.net_sales_usd)}</b><span>Net of refunds</span></div>
      <div className="stat"><b>{money(overview.refunded_usd)}</b><span>Refunded</span></div>
      <div className={`stat ${overview.pending_approvals ? 'warn' : ''}`}>
        <b>{overview.pending_approvals}</b><span>Pending approvals</span>
      </div>
      <div className={`stat ${overview.refund_requests_pending ? 'warn' : ''}`}>
        <b>{overview.refund_requests_pending}</b><span>Pending refund requests</span>
      </div>
      <div className={`stat ${overview.failed_transactions ? 'warn' : ''}`}>
        <b>{overview.failed_transactions}</b><span>Failed transactions</span>
      </div>
    </div>

    <h3 style={{ fontFamily: 'var(--display)', fontSize: 17, margin: '20px 0 8px' }}>Waiting for approval</h3>
    {approvals.length === 0 && <div className="empty">Nothing waiting for approval.</div>}
    {approvals.map((r) => (
      <div className="req" key={r.id}>
        <div className="tag">{r.kind === 'refund' ? 'Refund' : 'Large purchase'}</div>
        <b>{money(r.total_usd)} USD</b>
        <span>{r.summary}</span>
        <div className="btns">
          <button className="yes" onClick={() => onDecide(r.id, true)}>Approve</button>
          <button className="no" onClick={() => onDecide(r.id, false)}>Decline</button>
        </div>
      </div>
    ))}
    </>
  );
}

function OrdersSection({ orders }) {
  if (!orders.length) return <div className="empty">No orders yet.</div>;
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th>Order</th><th>Item</th><th>Customer</th><th>Total</th><th>Status</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.order_id}>
              <td><code>{o.order_id}</code></td>
              <td>{o.item}</td>
              <td>{o.customer_id}</td>
              <td>{money(o.total_usd)}</td>
              <td><span className={`chip ${o.status === 'COMPLETED' ? 'ok' : ['REFUNDED', 'FAILED'].includes(o.status) ? 'bad' : 'wait'}`}>{o.status}</span></td>
              <td>{when(o.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UsageSection({ usage }) {
  if (!usage) return <div className="empty">Loading…</div>;
  return (
    <>
      <div className="stats">
        <div className="stat"><b>{usage.conversations_total}</b><span>Conversations</span></div>
        <div className="stat"><b>{usage.conversations_active}</b><span>Active now</span></div>
        <div className="stat"><b>{usage.messages_total}</b><span>Messages stored</span></div>
        <div className="stat"><b>{usage.messages_last_7_days}</b><span>Last 7 days</span></div>
        <div className="stat"><b>{usage.tool_calls_total}</b><span>Tool calls</span></div>
      </div>
      <h3 style={{ fontFamily: 'var(--display)', fontSize: 17, margin: '20px 0 8px' }}>Tools used by the agent</h3>
      {usage.tools.length === 0 ? (
        <div className="empty">No tool calls yet.</div>
      ) : (
        <div className="tablewrap">
          <table className="table">
            <thead><tr><th>Tool</th><th>Calls</th></tr></thead>
            <tbody>
              {usage.tools.map((tool) => (
                <tr key={tool.name}><td><code>{tool.name}</code></td><td>{tool.calls}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function Owner() {
  const [owner, setOwner] = useState(null);
  const [setup, setSetup] = useState({ needs_setup: false, registration_open: false });
  const [booting, setBooting] = useState(true);
  const [tab, setTab] = useState('overview');
  const [mode, setMode] = useState('login');
  const [form, setForm] = useState({ email: '', password: '', name: '', store_name: '', signup_code: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const [items, setItems] = useState([]);
  const [orders, setOrders] = useState([]);
  const [refundList, setRefundList] = useState([]);
  const [notes, setNotes] = useState({});
  const [overview, setOverview] = useState(null);
  const [usage, setUsage] = useState(null);
  const [settings, setSettings] = useState(null);
  const [settingsForm, setSettingsForm] = useState(null);
  const [settingsMsg, setSettingsMsg] = useState('');
  const [sessions, setSessions] = useState([]);

  const [payments, setPayments] = useState({ supported: [], configured: [], active_environment: 'sandbox' });
  const [pay, setPay] = useState({ environment: 'sandbox', client_id: '', client_secret: '' });
  const [payError, setPayError] = useState('');
  const [payBusy, setPayBusy] = useState(false);

  const [pw, setPw] = useState({ current_password: '', new_password: '' });
  const [pwMsg, setPwMsg] = useState('');

  const loadData = useCallback(async () => {
    const [overviewRes, ordersRes, refundsRes, providersRes, usageRes, settingsRes, sessionsRes, approvalsRes] =
      await Promise.all([
        call('/owner/overview'),
        call('/owner/orders'),
        call('/owner/refund-requests'),
        call('/owner/payment-providers'),
        call('/owner/usage'),
        call('/owner/settings'),
        call('/owner/sessions'),
        call('/owner/approvals'),
      ]);

    if (overviewRes.status === 401) {
      setOwner(null);
      return;
    }
    if (overviewRes.ok) setOverview(await overviewRes.json());
    if (ordersRes.ok) setOrders(await ordersRes.json());
    if (refundsRes.ok) setRefundList(await refundsRes.json());
    if (providersRes.ok) setPayments(await providersRes.json());
    if (usageRes.ok) setUsage(await usageRes.json());
    if (sessionsRes.ok) setSessions(await sessionsRes.json());
    if (approvalsRes.ok) setItems(await approvalsRes.json());
    if (settingsRes.ok) {
      const data = await settingsRes.json();
      setSettings(data);
      setSettingsForm((prev) => prev || {
        name: data.name,
        max_order_usd: data.max_order_usd,
        max_auto_refund_usd: data.max_auto_refund_usd,
      });
    }
  }, []);

  useEffect(() => {
    (async () => {
      const status = await call('/auth/setup-status').then((r) => r.json()).catch(() => ({}));
      setSetup(status);
      setMode(status.needs_setup ? 'register' : 'login');
      const me = await call('/auth/me');
      if (me.ok) {
        const data = await me.json();
        setOwner(data.owner);
      }
      setBooting(false);
    })();
  }, []);

  useEffect(() => {
    if (!owner) return;
    loadData();
    const t = setInterval(loadData, 5000);
    return () => clearInterval(t);
  }, [owner, loadData]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await json(mode === 'register' ? '/auth/register' : '/auth/login', form);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Request failed');
      setOwner(data.owner);
      setForm({ email: '', password: '', name: '', store_name: '', signup_code: '' });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    await call('/auth/logout', { method: 'POST' });
    setOwner(null);
    setItems([]);
    setOrders([]);
    setRefundList([]);
  };

  const decide = async (id, approve) => {
    const res = await json(`/owner/approvals/${id}`, { approve });
    if (!res.ok) setError('Could not complete this action');
    loadData();
  };

  const refundAction = async (id, action, body = {}) => {
    setError('');
    const path =
      action === 'decision'
        ? `/owner/refund-requests/${id}/decision`
        : `/owner/refund-requests/${id}/request-evidence`;
    const res = await json(path, { ...body, note: notes[id] || '' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) setError(data.detail || 'Could not complete this action');
    loadData();
  };

  const purgeEvidence = async () => {
    const res = await call('/owner/refund-requests/purge', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (res.ok) setError(`Deleted ${data.deleted} old evidence file(s).`);
    loadData();
  };

  const saveSettings = async (e) => {
    e.preventDefault();
    setSettingsMsg('');
    const res = await patch('/owner/settings', {
      name: settingsForm.name,
      max_order_usd: Number(settingsForm.max_order_usd),
      max_auto_refund_usd: Number(settingsForm.max_auto_refund_usd),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setSettingsMsg(data.detail || 'Could not save these settings');
      return;
    }
    setSettings(data);
    setSettingsForm({
      name: data.name,
      max_order_usd: data.max_order_usd,
      max_auto_refund_usd: data.max_auto_refund_usd,
    });
    setSettingsMsg('Saved.');
    loadData();
  };

  const changePassword = async (e) => {
    e.preventDefault();
    setPwMsg('');
    const res = await json('/owner/account/password', pw);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setPwMsg(data.detail || 'Could not change the password');
      return;
    }
    setPw({ current_password: '', new_password: '' });
    setPwMsg(`Password changed. ${data.other_sessions_revoked} other session(s) signed out.`);
    loadData();
  };

  const revokeOthers = async () => {
    const res = await call('/owner/sessions/revoke-others', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (res.ok) setPwMsg(`Signed out ${data.revoked} other session(s).`);
    loadData();
  };

  const saveProvider = async (e) => {
    e.preventDefault();
    setPayBusy(true);
    setPayError('');
    try {
      const res = await json('/owner/payment-providers/paypal', pay);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not save these credentials');
      if (data.provider?.status !== 'operational') {
        setPayError(data.provider?.error || 'PayPal did not accept these credentials');
      }
      setPay({ ...pay, client_secret: '' });
      loadData();
    } catch (err) {
      setPayError(err.message);
    } finally {
      setPayBusy(false);
    }
  };

  const removeProvider = async (environment) => {
    setPayError('');
    await call(`/owner/payment-providers/paypal/${environment}`, { method: 'DELETE' });
    loadData();
  };

  if (booting) {
    return <main className="owner"><p>Loading…</p></main>;
  }

  if (!owner) {
    return (
      <main className="owner">
        <h1>{mode === 'register' ? 'Create the owner account' : 'Owner sign in'}</h1>
        <p>
          {mode === 'register'
            ? 'The first account becomes the owner of this store.'
            : 'Sign in to approve purchases and refunds.'}
        </p>
        {mode === 'login' && (
          <p style={{ color: '#1746d1', fontWeight: '600', fontSize: '14px', margin: '12px 0 24px', padding: '0 4px', textAlign: 'left', lineHeight: '1.5' }}>
            💡 <b>For testing and review:</b><br/>
            📧 <b>Email:</b>example@payagent.com<br/>
            🔑 <b>Password:</b> demo-pay00
          </p>
        )}

        <form onSubmit={submit}>
          <div className="tokenrow">
            <input
              type="email"
              placeholder="Email"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              autoComplete="username"
              required
            />
          </div>
          <div className="tokenrow">
            <input
              type="password"
              placeholder={mode === 'register' ? 'Password (min. 10 characters)' : 'Password'}
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
              required
            />
          </div>
          {mode === 'register' && (
            <>
              <div className="tokenrow">
                <input
                  type="text"
                  placeholder="Your name (optional)"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </div>
              <div className="tokenrow">
                <input
                  type="text"
                  placeholder="Store name (optional)"
                  value={form.store_name}
                  onChange={(e) => setForm({ ...form, store_name: e.target.value })}
                />
              </div>
              {setup.registration_open && !setup.needs_setup && (
                <div className="tokenrow">
                  <input
                    type="text"
                    placeholder="Signup code"
                    value={form.signup_code}
                    onChange={(e) => setForm({ ...form, signup_code: e.target.value })}
                  />
                </div>
              )}
            </>
          )}
          {error && <p style={{ color: '#a12020' }}>{error}</p>}
          <div className="btns" style={{ marginBottom: 18 }}>
            <button className="yes" type="submit" disabled={busy}>
              {busy ? 'Please wait…' : mode === 'register' ? 'Create account' : 'Sign in'}
            </button>
          </div>
        </form>
        {setup.registration_open && (
          <p>
            {mode === 'register' ? 'Already have an account?' : 'Need an account?'}{' '}
            <button
              type="button"
              onClick={() => { setMode(mode === 'register' ? 'login' : 'register'); setError(''); }}
              style={{ border: 0, background: 'none', color: '#1746d1', cursor: 'pointer', padding: 0, font: 'inherit' }}
            >
              {mode === 'register' ? 'Sign in' : 'Create account'}
            </button>
          </p>
        )}
      </main>
    );
  }

  return (
    <main className="owner dash">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12 }}>
        <div>
          <h1>{owner.store?.name || 'Owner panel'}</h1>
          <p style={{ margin: 0 }}>{owner.email}</p>
        </div>
        <button className="no" onClick={logout} style={{ border: 0, borderRadius: 12, padding: '9px 16px', font: '600 14px var(--body)', cursor: 'pointer' }}>
          Sign out
        </button>
      </div>

      {error && <p style={{ color: '#a12020' }}>{error}</p>}

      <nav className="tabs" role="tablist" aria-label="Dashboard sections">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className="tab"
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === 'overview' && (
        <OverviewSection overview={overview} approvals={items} onDecide={decide} />
      )}

      {tab === 'orders' && <OrdersSection orders={orders} />}

      {tab === 'refunds' && (
        <>
          <p className="hint">
            Only you can approve a refund. Inspect the photos or videos before deciding — the
            assistant cannot judge whether the evidence is genuine.
          </p>
          {refundList.length === 0 && <div className="empty">No refund requests yet.</div>}
          {refundList.map((r) => (
            <div className="req" key={r.id}>
              <div className="provrow">
                <div>
                  <span className={`chip ${refundTone(r.status)}`}>{r.status}</span>
                  <b>{money(r.amount_usd)} USD</b>
                  <span>{r.product_name}</span>
                </div>
                <span className="hint">{when(r.created_at)}</span>
              </div>

              <dl className="kv">
                <dt>Refund request</dt><dd>{r.id}</dd>
                <dt>Order</dt><dd>{r.order_id}</dd>
                <dt>Customer</dt><dd>{r.customer_id}</dd>
                <dt>Amount paid</dt><dd>{money(r.amount_paid_usd)} USD</dd>
                <dt>Refund requested</dt><dd>{money(r.amount_usd)} USD</dd>
                <dt>Purchase date</dt><dd>{when(r.purchase_date)}</dd>
                <dt>Requested at</dt><dd>{when(r.created_at)}</dd>
                <dt>Explanation</dt><dd>{r.reason || '—'}</dd>
                {r.failure_reason && <><dt>Failure</dt><dd style={{ color: '#a12020' }}>{r.failure_reason}</dd></>}
                {r.paypal_refund_id && <><dt>PayPal refund</dt><dd>{r.paypal_refund_id}</dd></>}
              </dl>

              {r.evidence.length > 0 ? (
                <div className="refundgrid">
                  {r.evidence.map((item) => (
                    <Evidence key={item.id} requestId={r.id} item={item} />
                  ))}
                </div>
              ) : (
                <p className="hint">No evidence uploaded yet.</p>
              )}

              {!REFUND_CLOSED.includes(r.status) && (
                <>
                  <textarea
                    rows={2}
                    placeholder="Note for the customer (optional)"
                    value={notes[r.id] || ''}
                    onChange={(e) => setNotes({ ...notes, [r.id]: e.target.value })}
                  />
                  <div className="btns">
                    {r.status === 'UNDER_REVIEW' && (
                      <button className="yes" onClick={() => refundAction(r.id, 'decision', { approve: true })}>
                        Approve refund
                      </button>
                    )}
                    <button className="no" onClick={() => refundAction(r.id, 'decision', { approve: false })}>
                      Reject
                    </button>
                    <button className="no" onClick={() => refundAction(r.id, 'request-evidence')}>
                      Request more evidence
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
          {refundList.length > 0 && (
            <button className="no" onClick={purgeEvidence} style={{ border: 0, borderRadius: 12, padding: '9px 16px', font: '600 14px var(--body)', cursor: 'pointer' }}>
              Clean up evidence past the retention window (30 days)
            </button>
          )}
        </>
      )}

      {tab === 'providers' && (
        <>
          <p className="hint">
            PayPal credentials are encrypted with AES-256-GCM and validated against PayPal before
            they are used. Only the last four characters are shown again. Active environment:{' '}
            <b>{payments.active_environment}</b>.
          </p>

          {payments.configured.map((p) => (
            <div className="req" key={`${p.provider}-${p.environment}`}>
              <div className="provrow">
                <div>
                  <div className="tag">{p.provider} · {p.environment}</div>
                  <b>{p.client_id}</b>
                  <span>Secret {p.client_secret_masked}</span>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <span className={`chip ${p.status === 'operational' ? 'ok' : p.status === 'invalid' ? 'bad' : 'wait'}`}>
                    {STATUS_LABEL[p.status] || p.status}
                  </span>
                  {p.error && <p className="hint" style={{ color: '#a12020', margin: '6px 0 0' }}>{p.error}</p>}
                  <div className="btns" style={{ justifyContent: 'flex-end' }}>
                    <button className="no" onClick={() => removeProvider(p.environment)}>Remove</button>
                  </div>
                </div>
              </div>
            </div>
          ))}

          <form className="req" onSubmit={saveProvider}>
            <div className="tag">PayPal</div>
            <p className="hint" style={{ marginTop: 6 }}>
              Create a REST app in the PayPal Developer dashboard and paste its keys here.
            </p>
            <div className="tokenrow">
              <select
                value={pay.environment}
                onChange={(e) => setPay({ ...pay, environment: e.target.value })}
                aria-label="PayPal environment"
              >
                {(payments.supported.find((s) => s.provider === 'paypal')?.environments || ['sandbox', 'live']).map((env) => (
                  <option key={env} value={env}>{env === 'live' ? 'Live' : 'Sandbox'}</option>
                ))}
              </select>
            </div>
            <div className="tokenrow">
              <input
                type="text"
                placeholder="Client ID"
                value={pay.client_id}
                onChange={(e) => setPay({ ...pay, client_id: e.target.value })}
                autoComplete="off"
                required
              />
            </div>
            <div className="tokenrow">
              <input
                type="password"
                placeholder="Client Secret"
                value={pay.client_secret}
                onChange={(e) => setPay({ ...pay, client_secret: e.target.value })}
                autoComplete="new-password"
                required
              />
            </div>
            {payError && <p style={{ color: '#a12020' }}>{payError}</p>}
            <div className="btns">
              <button className="yes" type="submit" disabled={payBusy}>
                {payBusy ? 'Validating…' : 'Save and validate'}
              </button>
            </div>
          </form>
        </>
      )}

      {tab === 'usage' && <UsageSection usage={usage} />}

      {tab === 'settings' && settingsForm && (
        <form className="req" onSubmit={saveSettings}>
          <h2 style={{ fontFamily: 'var(--display)', fontSize: 18, margin: '0 0 4px' }}>Store settings</h2>
          <p className="hint" style={{ marginTop: 4 }}>
            Limits are enforced on the backend for every purchase and refund the agent proposes.
          </p>
          <div className="tokenrow">
            <input
              type="text"
              placeholder="Store name"
              value={settingsForm.name}
              onChange={(e) => setSettingsForm({ ...settingsForm, name: e.target.value })}
              maxLength={80}
              required
            />
          </div>
          <div className="tokenrow">
            <input
              type="number"
              min="1"
              step="0.01"
              placeholder="Purchases above this need approval (USD)"
              value={settingsForm.max_order_usd}
              onChange={(e) => setSettingsForm({ ...settingsForm, max_order_usd: e.target.value })}
              required
            />
          </div>
          <div className="tokenrow">
            <input
              type="number"
              min="0"
              step="0.01"
              placeholder="Automatic refund limit (USD)"
              value={settingsForm.max_auto_refund_usd}
              onChange={(e) => setSettingsForm({ ...settingsForm, max_auto_refund_usd: e.target.value })}
              required
            />
          </div>
          <p className="hint">Currency: {settings?.currency || 'USD'} (fixed in this version).</p>
          {settingsMsg && (
            <p className="hint" style={{ color: settingsMsg === 'Saved.' ? 'var(--ok)' : '#a12020' }}>{settingsMsg}</p>
          )}
          <div className="btns">
            <button className="yes" type="submit">Save settings</button>
          </div>
        </form>
      )}

      {tab === 'security' && (
        <>
          <form className="req" onSubmit={changePassword}>
            <h2 style={{ fontFamily: 'var(--display)', fontSize: 18, margin: '0 0 4px' }}>Change password</h2>
            <p className="hint" style={{ marginTop: 4 }}>Other sessions are signed out when the password changes.</p>
            <div className="tokenrow">
              <input
                type="password"
                placeholder="Current password"
                value={pw.current_password}
                onChange={(e) => setPw({ ...pw, current_password: e.target.value })}
                autoComplete="current-password"
                required
              />
            </div>
            <div className="tokenrow">
              <input
                type="password"
                placeholder="New password (min. 10 characters)"
                value={pw.new_password}
                onChange={(e) => setPw({ ...pw, new_password: e.target.value })}
                autoComplete="new-password"
                required
              />
            </div>
            {pwMsg && <p className="hint" style={{ color: pwMsg.startsWith('Password changed') ? 'var(--ok)' : '#a12020' }}>{pwMsg}</p>}
            <div className="btns">
              <button className="yes" type="submit">Change password</button>
            </div>
          </form>

          <h2 style={{ fontFamily: 'var(--display)', fontSize: 18, margin: '20px 0 8px' }}>Active sessions</h2>
          <div className="tablewrap">
            <table className="table">
              <thead><tr><th>Session</th><th>Started</th><th>Last seen</th><th>IP</th></tr></thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.id}>
                    <td><code>{s.id}</code> {s.current && <span className="chip ok">this device</span>}</td>
                    <td>{when(s.created_at)}</td>
                    <td>{when(s.last_seen_at)}</td>
                    <td>{s.ip || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="btns" style={{ marginTop: 12 }}>
            <button className="no" onClick={revokeOthers}>Sign out other sessions</button>
          </div>
        </>
      )}
    </main>
  );
}
