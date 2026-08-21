# Security

This repository is an alpha, local-first finance workspace. Do not expose the built-in development server directly to the public internet or use it as the sole system of record for bank payments, accounting, or tax filing.

Please report vulnerabilities privately to the repository maintainers. Do not include real invoices, payroll records, bank statements, credentials, tax IDs, personal data, or customer data in a public issue. A useful report includes the affected version, reproduction steps using fictional data, impact, and a suggested mitigation when available.

Before a multi-user production deployment, add authenticated roles and segregation of duties, encrypted storage and transport, secrets management, malware scanning, a transactional database with migrations, backup and recovery testing, immutable audit retention, monitoring, and rate/request-size limits.

The built-in server binds to `127.0.0.1` by default. `OPC_FINANCE_API_TOKEN` enables one legacy admin principal. Prefer `OPC_FINANCE_API_AUTH_FILE` for hashed-token reader/operator/reviewer/admin principals and operator/reviewer separation; the policy file must be private to its owner. Every finance API except `/api/health` then requires `Authorization: Bearer <token>`. Binding `OPC_FINANCE_HOST` to a non-loopback address without an authentication policy fails closed; authentication does not replace TLS, a reverse proxy, network policy, SSO, organizational role governance, or immutable external audit retention.
