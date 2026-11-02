// The chat session lives in localStorage while the buyer is away on PayPal:
// both the chat page and /success read and write the same copy through here.
export const CHAT_SESSION_KEY = 'payagent_chat_session';
// The order waiting to be paid, set when the buyer leaves for checkout.
export const PENDING_ORDER_KEY = 'payagent_pending_order';

export const GREETING = "Hi, I'm PayAgent. Tell me what you need and I'll find it and set up the payment.";

export const PAID_MESSAGE =
    '✨ Your payment was successfully confirmed via PayPal! Your order is now being processed. How can I help you next?';

export const loadChatSession = () => {
  try {
    const saved = JSON.parse(localStorage.getItem(CHAT_SESSION_KEY));
    if (Array.isArray(saved) && saved.length) return saved;
  } catch {}
  return null;
};

export const saveChatSession = (messages) => {
  try { localStorage.setItem(CHAT_SESSION_KEY, JSON.stringify(messages)); } catch {}
};

export const loadPendingOrder = () => {
  try { return localStorage.getItem(PENDING_ORDER_KEY) || null; } catch { return null; }
};

export const savePendingOrder = (orderId) => {
  try {
    if (orderId) localStorage.setItem(PENDING_ORDER_KEY, orderId);
    else localStorage.removeItem(PENDING_ORDER_KEY);
  } catch {}
};
