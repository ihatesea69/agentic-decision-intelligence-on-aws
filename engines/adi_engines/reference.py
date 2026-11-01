"""Synthetic demo sample: invented numbers, not data from any lender.

Tests and the offline run use these figures. Every derived value below is computed by hand from
DEMO_COUNTS, so a test that reproduces it checks the engines, not a lookup.
To check the engines against a private published sample, see tests/test_private_reference.py.
"""
SAMPLE_MONTH = "2030-01"

#              B0   B1  B2  B3  B4  PP  WO
DEMO_COUNTS = [[812, 38, 0, 0, 0, 5, 0],     # B0, 855 accounts
               [29, 24, 17, 1, 0, 0, 0],     # B1, 71 (one B1 -> B3 jump: a DQ exception)
               [0, 9, 11, 16, 0, 0, 0],      # B2, 36
               [0, 0, 5, 4, 10, 0, 0],       # B3, 19 (sparse)
               [0, 0, 0, 2, 3, 0, 14]]       # B4, 19 (sparse)
DEMO_ROW_TOTALS = [855, 71, 36, 19, 19]
DEMO_ENDING = [841, 71, 33, 23, 13, 5, 14]     # column totals, 1,000 accounts

DEMO_ROLLED_FORWARD = 96
DEMO_RECOVERED = 45
DEMO_NET_DETERIORATION = 0.051
DEMO_FORWARD_ROLL = {"B1": 0.254, "B2": 0.444, "B3": 0.526, "B4": 0.737}
DEMO_CURE = {"B1": 0.408, "B4": 0.105}
DEMO_B4_WO_INTERVAL = (0.512, 0.882)            # Wilson 95%, 14 of 19

# One invented month of KPI lines (VND bn). The headline is deliberately NOT the sum of the lines,
# so the reconciliation check has something to catch: lines give 0.17, the headline says 0.25.
DEMO_KPIS = {
    "interest_revenue": 1.90,
    "fee_income": 0.10,
    "total_portfolio_revenue": 2.00,
    "expected_credit_loss": 2.60,
    "provision_expense": 0.15,
    "write_off": 1.10,
    "collection_cost": 0.10,
    "cost_of_funds": 0.48,
    "risk_adjusted_net_profit": 0.25,
}
