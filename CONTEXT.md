# Project Context

This file tracks the history of changes, decisions, and current state of the project.

---

## [2026-03-24 16:50] — Meshtastic LLM Bridge Integration

- What changed:
  - Added full Meshtastic mesh network integration to Project N.O.M.A.D.
  - **Backend (AdonisJS):**
    - Database migrations for `mesh_nodes` and `mesh_messages` tables
    - Lucid models: `MeshNode`, `MeshMessage`
    - `MeshtasticService` — core business logic (incoming message processing, node management, config via KV store, outgoing message polling)
    - `MeshLLMJob` — BullMQ job for async LLM processing with RAG support and timeout handling
    - `MeshtasticController` — full REST API (10 endpoints under `/api/meshtastic`)
    - Vine validators for incoming messages, node updates, and config changes
    - Routes added to `routes.ts`, settings controller method added
    - Constants updated: `service_names.ts`, `broadcast.ts`, `kv_store.ts`, `meshtastic.ts` (new)
    - Types added: `types/meshtastic.ts`
  - **Python Sidecar (`meshtastic-bridge/`):**
    - `bridge.py` — main entry point, connects to Meshtastic radio (serial/TCP), message loop
    - `protocol.py` — 8-byte header chunking protocol for LoRa's 228-byte limit, zlib compression
    - `nomad_client.py` — HTTP client for N.O.M.A.D. API with offline queue
    - `config.py` — configuration from env vars + API polling
    - `health.py` — Flask health check server
    - `Dockerfile` — Python 3.11 slim with meshtastic, requests, flask
    - `tests/test_protocol.py` — 14 unit tests (all passing)
  - **Frontend (React):**
    - Settings page at `/settings/meshtastic` with connection config, LLM settings, node management, activity feed
    - API methods added to `api.ts` for all meshtastic endpoints
    - Navigation link added to `SettingsLayout.tsx`
  - **README.md** updated with Meshtastic Bridge documentation

- Why:
  - Extends N.O.M.A.D.'s offline AI capabilities beyond WiFi range using LoRa mesh networking
  - In disaster/survival scenarios, people miles away can query the LLM via cheap Meshtastic radios

- Impact on project goals:
  - Adds a major new capability (mesh radio AI access) while maintaining the offline-first architecture
  - Follows existing patterns (Docker sidecar, BullMQ jobs, KV store config, Transmit broadcasts)

- Files modified:
  - `admin/constants/service_names.ts` — added MESHTASTIC
  - `admin/constants/broadcast.ts` — added mesh broadcast channels
  - `admin/types/kv_store.ts` — added meshtastic config keys
  - `admin/constants/kv_store.ts` — added meshtastic settings keys
  - `admin/start/routes.ts` — added meshtastic routes + settings route
  - `admin/app/controllers/settings_controller.ts` — added meshtastic() method
  - `admin/inertia/layouts/SettingsLayout.tsx` — added Meshtastic nav link
  - `admin/inertia/lib/api.ts` — added meshtastic API methods
  - `README.md` — added Meshtastic Bridge section

- Files created:
  - `admin/database/migrations/1772000000001_create_mesh_nodes_table.ts`
  - `admin/database/migrations/1772000000002_create_mesh_messages_table.ts`
  - `admin/app/models/mesh_node.ts`
  - `admin/app/models/mesh_message.ts`
  - `admin/app/services/meshtastic_service.ts`
  - `admin/app/controllers/meshtastic_controller.ts`
  - `admin/app/validators/meshtastic.ts`
  - `admin/app/jobs/mesh_llm_job.ts`
  - `admin/types/meshtastic.ts`
  - `admin/constants/meshtastic.ts`
  - `admin/inertia/pages/settings/meshtastic.tsx`
  - `meshtastic-bridge/` (entire directory: bridge.py, protocol.py, nomad_client.py, config.py, health.py, Dockerfile, requirements.txt, tests/)
  - `CONTEXT.md`
