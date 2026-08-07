import { redirect } from "next/navigation";

import { Footer } from "@/components/landing/footer";
import { Header } from "@/components/landing/header";
import { Hero } from "@/components/landing/hero";
import { CaseStudySection } from "@/components/landing/sections/case-study-section";
import { CommunitySection } from "@/components/landing/sections/community-section";
import { SandboxSection } from "@/components/landing/sections/sandbox-section";
import { SkillsSection } from "@/components/landing/sections/skills-section";
import { WhatsNewSection } from "@/components/landing/sections/whats-new-section";
import { assertNever } from "@/core/auth/types";
import { getServerSideUser } from "@/core/auth/server";
import { env } from "@/env";

export const dynamic = "force-dynamic";

export default async function LandingPage() {
  if (env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true") {
    // The public welcome page is disabled: authenticated users land directly in
    // the workspace, everyone else is sent to the login page.
    const result = await getServerSideUser();

    switch (result.tag) {
      case "authenticated":
        redirect("/workspace");
      case "needs_setup":
      case "system_setup_required":
        redirect("/setup");
      case "unauthenticated":
      case "gateway_unavailable":
        // The login page renders the gateway-offline fallback banner itself.
        redirect("/login");
      case "config_error":
        throw new Error(result.message);
      default:
        assertNever(result);
    }
  }

  // Static-website demo mode keeps the landing page shell (header/footer) but
  // the welcome hero is not part of the platform.
  return (
    <div className="min-h-screen w-full overflow-x-clip bg-[#0a0a0a]">
      <Header />
      <main className="flex w-full flex-col">
        <Hero />
        <CaseStudySection />
        <SkillsSection />
        <SandboxSection />
        <WhatsNewSection />
        <CommunitySection />
      </main>
      <Footer />
    </div>
  );
}
