# BMA Health Database — DEPRECATED

> **This project is deprecated and no longer maintained.**
>
> **Use [`bma-dms-backend`](../bma-dms-backend/) instead** (Fastify + MongoDB on port 3001).
>
> Local path: `/Users/dev/bma-dms-backend/`

---

## Status

- Database container, volumes, source CSVs, and `.env` have been removed.
- No further development is planned. Code remains in git history for reference only.
- Do **not** add features, fix bugs, or open PRs against this repository.

## Stack comparison

|             | This repo (deprecated) | Replacement (`bma-dms-backend`) |
|-------------|------------------------|---------------------------------|
| Framework   | FastAPI (Python)       | Fastify (TypeScript, Node 22)   |
| Database    | PostgreSQL 16          | MongoDB 7 (replica set)         |
| Port        | 9002                   | 3001                            |

## If you came here looking for the BMA health API

Go to `/Users/dev/bma-dms-backend/` and follow its README.
