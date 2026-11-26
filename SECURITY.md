# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button (Security → Advisories) on this repository rather than in a public issue. Include what you found, how to reproduce it, and the impact you expect. You should get a response within a week.

## Scope

Of particular interest:

- Ways for an agent inside the environment to read or alter recordings, read the model credential, or carry state across attempts.
- Ways to reach the model proxy from outside the host, or to make it forward requests other than model calls.
- Ways for a rebuild to pass silently when the rebuilt environment differs from the original run.

The README's "Isolation limits" lists known limits that are not considered vulnerabilities (for example, the agent can reach the proxies, and the clock file is writable inside the container).
