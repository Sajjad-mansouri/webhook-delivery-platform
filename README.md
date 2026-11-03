# Webhook Delivery Platform

A multi-tenant webhook delivery service built with **FastAPI**, **PostgreSQL**, and **async SQLAlchemy**. The platform allows authenticated tenants to register webhook endpoints, submit events, and track reliable event delivery through an independent background worker.

## Overview

Webhook Delivery Platform provides an API-driven infrastructure for receiving events and delivering them to external HTTP endpoints.

It is designed around reliable delivery, tenant isolation, controlled retries, request authentication, and delivery history. The system separates event ingestion from webhook execution, allowing delivery processing to operate independently of incoming API requests.

## Core Features

### Multi-Tenant Authentication and Isolation

- API key authentication using the `X-API-Key` request header.
- Tenant identification through authenticated API keys.
- Tenant-scoped access to application resources.
- Protected endpoint and delivery management operations.

### Webhook Endpoint Management

- Register and manage webhook endpoints.
- Validate endpoint configuration and URLs.
- Associate endpoints with their respective tenants.
- Protect webhook signing secrets using encryption.

### Event Ingestion

- Accept events through REST API endpoints.
- Associate incoming events with the authenticated tenant.
- Create delivery records for registered endpoints.
- Support idempotency to prevent duplicate event processing.

### Reliable Delivery Processing

- Process webhook deliveries through an independent worker.
- Claim pending deliveries using database-backed coordination.
- Use lease tokens and expiration timestamps to manage delivery ownership.
- Execute outbound HTTP requests and record their results.
- Maintain individual delivery attempt records.

### Retry Mechanism

- Classify HTTP responses according to delivery outcome.
- Treat successful 2xx responses as successful deliveries.
- Handle retryable responses, including HTTP 408, 429, and 5xx.
- Apply exponential backoff with jitter.
- Enforce a configurable maximum number of delivery attempts.

### Delivery Recovery

- Detect deliveries left in processing state after worker interruption.
- Recover deliveries with expired processing leases.
- Prevent stale workers from modifying deliveries they no longer own.
- Preserve delivery state and attempt history.

### Webhook Security

- Generate webhook signatures for outgoing requests.
- Protect endpoint signing secrets.
- Support verification of webhook authenticity by receiving systems.

### Delivery History and Replay

- Retrieve tenant-scoped delivery records.
- Inspect individual delivery attempts.
- Preserve the history of delivery execution.
- Replay failed deliveries by creating a new delivery linked to its original delivery.

### API Error Handling

- Centralized application error handling.
- Consistent error response structure.
- Separate handling for application errors and HTTP exceptions.

## Architecture

The application separates HTTP request handling, persistent data management, and background delivery execution.

```text
                    External Clients
                           |
                           |
                    FastAPI Application
                           |
              +------------+------------+
              |            |            |
          Authentication  Events    Endpoints
              |            |            |
              +------------+------------+
                           |
                       PostgreSQL
                           |
                    Delivery Records
                           |
                     Background Worker
                           |
                  Outbound HTTP Requests
                           |
                  Registered Webhooks
```

### Main Components

| Component | Responsibility |
|---|---|
| FastAPI | Exposes the REST API and handles incoming requests. |
| PostgreSQL | Stores tenants, API keys, endpoints, events, deliveries, and attempts. |
| Async SQLAlchemy | Provides asynchronous database access and ORM operations. |
| Background Worker | Claims and executes pending webhook deliveries. |
| Retry Policy | Determines whether failed deliveries should be retried and when. |
| Delivery History | Stores delivery results and individual execution attempts. |

## Technology Stack

| Technology | Purpose |
|---|---|
| Python | Primary programming language |
| FastAPI | API framework |
| PostgreSQL | Relational database |
| SQLAlchemy | ORM and database interaction |
| Async SQLAlchemy | Asynchronous database operations |
| asyncpg | PostgreSQL asynchronous driver |
| Pydantic | Data validation and serialization |
| HTTPX | Outbound HTTP requests |
| uv | Python dependency and project management |
| pytest | Automated testing |
| Ruff | Linting and code quality |

## Testing

The project includes automated tests covering application behavior across different layers.

Test coverage includes:

- API key authentication and tenant isolation.
- Event ingestion and idempotency.
- Endpoint validation.
- Database transaction behavior.
- Delivery claiming and execution.
- Retry classification and scheduling.
- Lease expiration and recovery.
- Webhook signature generation.
- Delivery history and replay.
- Error handling and security-related behavior.

The test suite uses **pytest** and includes unit, integration, and API-level tests.

## Project Structure

The application is organized around separate responsibilities, including API routes, database models, schemas, security, delivery processing, and configuration.

The test suite is also separated by testing scope:

```text
tests/
├── unit/
├── integration/
└── api/
```

## Running the Project

The project uses `uv` for dependency management.

Install dependencies:

```bash
uv sync
```

Configure the required environment variables, including database connection settings and security-related configuration.

Run the application using the project's configured ASGI entry point:

```bash
uv run uvicorn app.main:app --reload
```

Run automated tests:

```bash
uv run pytest
```

The Uvicorn command assumes the application is exposed as `app.main:app`.

## Engineering Considerations

Several backend engineering concerns are addressed in the implementation:

- Tenant isolation at the application and database-access layers.
- Idempotent event ingestion.
- Database-backed coordination between worker processes.
- Lease-based delivery ownership.
- Retry scheduling with exponential backoff and jitter.
- Recovery from interrupted delivery processing.
- Protection of webhook signing secrets.
- Consistent API error responses.
- Persistent delivery and attempt history.
- Automated tests for critical application behavior.

## What This Project Demonstrates

- Building asynchronous REST APIs with FastAPI.
- Designing multi-tenant backend systems.
- Working with PostgreSQL and asynchronous SQLAlchemy.
- Implementing API key authentication and authorization.
- Designing reliable background processing.
- Handling retries, idempotency, and failure recovery.
- Managing concurrent delivery execution.
- Implementing webhook signatures and secret protection.
- Writing unit, integration, and API tests.

