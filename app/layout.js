export const metadata = {
  title: "StudAI",
  description: "AI math tutor"
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
