import type { Metadata } from "next";
import { AuthProvider } from "@/context/AuthContext";
import "./globals.css";

export const metadata: Metadata = {
  title: "RESOLVE — From payment completed to problem solved",
  description:
    "RESOLVE is the settlement layer for agent-to-agent commerce disputes: a bounded autonomous agent that recovers what a purchase was actually supposed to deliver.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="font-body bg-surface text-text-charcoal antialiased">
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
