import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { ProblemSection } from "@/components/landing/ProblemSection";
import { HowItWorks } from "@/components/landing/HowItWorks";
import { SafetyGrid } from "@/components/landing/SafetyGrid";
import { ForMerchants } from "@/components/landing/ForMerchants";
import { ClosingCTA } from "@/components/landing/ClosingCTA";
import { Footer } from "@/components/landing/Footer";

export default function LandingPage() {
  return (
    <main className="min-h-screen">
      <Navbar />
      <Hero />
      <ProblemSection />
      <HowItWorks />
      <SafetyGrid />
      <ForMerchants />
      <ClosingCTA />
      <Footer />
    </main>
  );
}
