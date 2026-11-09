export function Footer() {
  return (
    <footer className="border-t border-text-charcoal/8 bg-surface px-6 py-16 lg:px-10">
      <div className="mx-auto max-w-6xl">
        <div className="flex flex-col justify-between gap-10 md:flex-row">
          <div className="max-w-xs">
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary-rose font-display text-xs text-surface">
                R
              </span>
              <span className="font-display text-lg">RESOLVE</span>
            </div>
            <p className="mt-3 text-sm text-text-charcoal/55">
              The settlement layer for agent-to-agent commerce disputes.
            </p>
          </div>

          <div className="grid grid-cols-3 gap-10 text-sm">
            <div>
              <p className="font-medium text-text-charcoal/80">Product</p>
              <ul className="mt-3 space-y-2 text-text-charcoal/55">
                <li>How it works</li>
                <li>Pricing</li>
                <li>Docs</li>
              </ul>
            </div>
            <div>
              <p className="font-medium text-text-charcoal/80">Company</p>
              <ul className="mt-3 space-y-2 text-text-charcoal/55">
                <li>About</li>
                <li>Careers</li>
              </ul>
            </div>
            <div>
              <p className="font-medium text-text-charcoal/80">Support</p>
              <ul className="mt-3 space-y-2 text-text-charcoal/55">
                <li>Help Center</li>
                <li>Contact</li>
              </ul>
            </div>
          </div>
        </div>

        <div className="mt-12 flex flex-col justify-between gap-4 border-t border-text-charcoal/8 pt-6 text-xs text-text-charcoal/45 md:flex-row">
          <p>© 2026 RESOLVE. Built on Razorpay.</p>
          <div className="flex gap-6">
            <span>Privacy Policy</span>
            <span>Terms of Service</span>
          </div>
        </div>
      </div>
    </footer>
  );
}
