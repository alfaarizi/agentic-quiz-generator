# Architecture Description

Date: 2026-06-28

This describes the target architecture for Agentic Quiz Generator as a retention-focused assessment SaaS.

## System Context

```mermaid
flowchart LR
    Learner[Learners]
    Course[Universities and course teams]
    Enterprise[Enterprise training teams]

    SaaS[Agentic Quiz Generator SaaS]

    Sources[Source material<br/>notes, slides, docs, diagrams]
    LLM[LLM provider<br/>OpenAI or Bedrock]
    Stripe[Stripe]
    Auth[OAuth provider]
    Obs[Langfuse / monitoring]

    Learner --> SaaS
    Course --> SaaS
    Enterprise --> SaaS
    Sources --> SaaS
    SaaS --> LLM
    SaaS --> Stripe
    SaaS --> Auth
    SaaS --> Obs
```

Users bring trusted learning material and objectives. The product turns them into assessments, feedback, scheduled reattempts, and gap reports. External systems handle identity, billing, model calls, and observability.

## Containers

```mermaid
flowchart TB
    Browser[React + TypeScript web app]

    Java[Java Spring Boot modular monolith<br/>accounts, tenants, billing, assessments, progress, GraphQL]
    Python[Python AI engine<br/>LangGraph, retrieval, ingestion, scoring, evaluation, MCP tools]

    Postgres[(PostgreSQL + pgvector<br/>app schema + vector schema)]
    Redis[(Redis<br/>cache, rate limits, sessions)]
    Rabbit[(RabbitMQ<br/>retryable jobs)]
    ObjectStore[(S3-compatible object storage<br/>uploads, images, exports)]

    LLM[OpenAI / Bedrock]
    Stripe[Stripe]
    Obs[Prometheus, Grafana, OpenTelemetry, Langfuse]

    Browser -->|GraphQL / SSE| Java
    Java -->|gRPC| Python
    Java --> Postgres
    Python --> Postgres
    Java --> Redis
    Java --> Rabbit
    Python --> Rabbit
    Java --> ObjectStore
    Python --> ObjectStore
    Python --> LLM
    Java --> Stripe
    Java --> Obs
    Python --> Obs
```

The Java monolith owns product state and tenant boundaries. The Python engine owns AI work. They share the PostgreSQL cluster but not table ownership: Java owns app data, Python owns vector and AI working data.

## Components

```mermaid
flowchart LR
    subgraph Web[Web App]
        Intake[Source intake]
        Runner[Assessment runner]
        Results[Feedback and gaps]
        Admin[Workspace admin]
    end

    subgraph Core[Spring Boot Modular Monolith]
        Identity[Identity and tenants]
        Billing[Billing and metering]
        Content[Source and objectives]
        Assessment[Assessment sessions]
        Retention[Scheduled reattempts]
        Progress[Progress and gap reports]
        API[GraphQL / REST]
    end

    subgraph AI[Python AI Engine]
        Ingestion[Document ingestion]
        Retrieval[Retrieval and embeddings]
        Generator[Question generation]
        Scorer[Answer scoring]
        Coverage[Coverage analysis]
        Eval[Evaluation harness]
        Tools[MCP tools]
    end

    Web --> API
    API --> Identity
    API --> Content
    API --> Assessment
    Assessment --> Retention
    Assessment --> Progress
    Billing --> Identity
    Content --> Ingestion
    Assessment --> Generator
    Assessment --> Scorer
    Progress --> Coverage
    Generator --> Retrieval
    Scorer --> Retrieval
    Coverage --> Retrieval
    Generator --> Tools
    Scorer --> Tools
    Coverage --> Tools
    Eval --> Generator
    Eval --> Scorer
    Eval --> Coverage
```

The core modules keep SaaS rules in Java. The AI engine returns generated questions, scores, explanations, retrieval evidence, and coverage findings. The monolith decides what becomes visible to users.

## Data Flow

```mermaid
sequenceDiagram
    actor User
    participant Web as React Web App
    participant Core as Java Monolith
    participant Queue as RabbitMQ
    participant AI as Python AI Engine
    participant DB as PostgreSQL / pgvector
    participant Store as Object Storage
    participant LLM as LLM Provider

    User->>Web: Upload source and objectives
    Web->>Core: Submit intake
    Core->>Store: Store source files
    Core->>DB: Save tenant, source, objective metadata
    Core->>Queue: Enqueue ingestion job
    AI->>Queue: Consume ingestion job
    AI->>Store: Read source files
    AI->>LLM: Embed and process content
    AI->>DB: Store chunks and embeddings

    User->>Web: Start assessment
    Web->>Core: Request assessment
    Core->>AI: Generate aligned questions
    AI->>DB: Retrieve relevant chunks
    AI->>LLM: Generate and validate questions
    AI-->>Core: Questions with source/objective/format metadata
    Core->>DB: Save assessment session
    Core-->>Web: Render mixed question formats

    User->>Web: Answer and rate confidence
    Web->>Core: Submit answers
    Core->>AI: Score open-ended answers and analyze gaps
    AI->>LLM: Score with rubric and evidence
    AI-->>Core: Feedback, score, misconceptions, gaps
    Core->>DB: Save results and scheduled reattempt
    Core-->>Web: Show feedback and gap report

    User->>Web: Return 7-14 days later
    Web->>Core: Complete scheduled reattempt
    Core->>DB: Compare first attempt vs reattempt
    Core-->>Web: Show retention progress
```

The key learning event is the scheduled reattempt: the user answers later, not rereads. Progress and gap reports are based on source-aligned answers over time.

## Architecture Decisions

- [ADR-001](./adr/adr-001-retention-first-learning-loop.md): Retention-first learning loop.
- [ADR-002](./adr/adr-002-system-architecture.md): Modular monolith with Python AI engine.
- [ADR-003](./adr/adr-003-contract-first-interfaces.md): Contract-first interfaces.
- [ADR-004](./adr/adr-004-data-and-background-work.md): PostgreSQL, Redis, pgvector, and RabbitMQ.
- [ADR-005](./adr/adr-005-assessment-quality-and-ai-evaluation.md): Assessment validity and AI evaluation gates.
- [ADR-006](./adr/adr-006-security-tenancy-and-billing.md): Security, tenancy, and billing.
- [ADR-007](./adr/adr-007-observability-and-slos.md): Observability, SLOs, and AI tracing.
- [ADR-008](./adr/adr-008-delivery-and-infrastructure.md): Docker Compose, ECS, Terraform, and CI gates.
