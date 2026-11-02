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
      <head>
        {/* Força o ícone PNG padrão */}
        <link rel="icon" type="image/png" href="/icon.png?v=2" />
        
        {/* Atalho alternativo que navegadores mobile adoram ler para atalhos rápidos */}
        <link rel="shortcut icon" type="image/png" href="/icon.png?v=2" />
        
        {/* Caso um dia queira suporte perfeito para telas Apple/iOS */}
        <link rel="apple-touch-icon" href="/icon.png?v=2" />
      </head>
      <body>{children}</body>
    </html>
  );
}

