import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "makhantaxwell | Automated GST & Bank Statement Conversion",
  description: "makhantaxwell helps e-commerce sellers and accountants automate GST filing and bank statement conversion to Tally XML. Save hours of manual work.",
  keywords: "GST automation, bank statement to tally, tally xml converter, e-commerce accounting, makhantaxwell clone",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        <link rel="icon" href="/favicon.ico" />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
