# Startup Idea -> MVP -> Investor Matching Pipeline pools.
#
# Both pools start empty on purpose — every entry used to be fully
# fabricated (fake founders with invented bios, fake VC personas with
# invented firms/bios). The fake investors were a real functional bug, not
# just cosmetic: Agent 4's scoring (rule-based fallback scores designations,
# and Groq itself tends to favor senior-sounding titles) meant "Managing
# Director" / "Partner" seed personas always outranked any real investor,
# so a real investor could never actually become a founder's top match while
# the fake ones existed alongside them.
#
# Both pools are now only ever populated by real users going through
# onboarding (agents/agent2.py), so nothing shown as a "match" is invented.
IDEAS_POOL = []
INVESTORS_POOL = []
