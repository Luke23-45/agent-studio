"""
Governance plane (Phase 5): compiled tenant config, tool gate, isolation
primitives, RLS support, evidence packets, compliance posture, residency.

Per the P0-2 ratified layout this package is the home for everything the
governance plane owns: compiled per-surface configuration (P5-1), the
tool authorization gate (P5-3), tenant-context isolation primitives
(P5-5), Postgres RLS support (P5-6), USD budget composition (P5-7),
evidence packet building (P5-9), compliance posture (P5-11) and
deployment-shape/residency helpers (P5-12).
"""
