export const metadata = {
  title: 'PayAgent - AI shopping assistant with PayPal',
  description: 'An open-source AI sales agent with PayPal checkout, refunds and human approval.',
};

export const viewport = {
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
