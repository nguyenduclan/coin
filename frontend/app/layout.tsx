import "./globals.css";
import Shell from "./Shell";

export const metadata = { title: "Coin Assistant" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="vi">
      <body><Shell>{children}</Shell></body>
    </html>
  );
}
