# Security policy

fints2camt handles online-banking credentials and account statements. Its security model
is described in the [README](README.md#security-model).

## Supported versions

Security fixes are made for the latest released minor version. Please update to the
latest release before reporting.

## Reporting a vulnerability

Please do **not** open a public issue for a vulnerability. Report it privately to:

**security@ext.dev**

Include what you found, how to reproduce it, and which version you use. **Never send a
PIN, TAN, token, FinTS product id or real account data** — a description or a synthetic
example is enough.

We acknowledge a report within a few working days, keep you informed about the fix, and
credit you in the changelog if you wish.

## Scope

In scope: the code in this repository and the container image built from it — for
example a way to make fints2camt log or send the PIN, a token or statement data anywhere
other than the bank and the configured targets, or to make it accept an unencrypted or
unintended target.

Out of scope: the banks' FinTS servers, `python-fints` and other dependencies (please
report to them directly), and the host fints2camt runs on.
