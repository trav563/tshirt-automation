#!/bin/bash
# Double-click to pull fresh data now (same as the nightly job): Shopify + Printify
# + Meta into Supabase, then regenerate the recommendations.
cd "$(dirname "$0")" || exit 1
.venv/bin/tshirt-nightly && .venv/bin/tshirt-recommend
echo ""
echo "Done. You can close this window."
