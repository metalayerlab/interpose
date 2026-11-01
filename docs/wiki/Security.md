# Security

## Threat model

<!-- guidance: what this protects, from whom, and what is out of scope. What's exposed (ports, endpoints), who should be able to reach it, what it trusts (headers, upstreams, input files), and what an attacker gains if it is compromised. -->

## Secrets

- The library never reads, logs or stores credentials on its own: callers pass them in.
- CI scans every commit with gitleaks, and secrets in history fail the build.

<!-- guidance: list each secret, what it grants, and how to rotate it. -->

## Hardening

- **Supply chain:** dependencies are pinned in the lock file and updated weekly by Dependabot. GitHub Actions are pinned to commit SHAs.

<!-- guidance: input validation, authentication and authorisation, rate limiting: what this project does and where. -->

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/metalayerlab/interpose/security/advisories/new), never in a public issue.
