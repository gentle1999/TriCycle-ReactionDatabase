# Frontend Refactor Plan

[中文](../frontend-refactor-plan.md) | [Documentation index](README.md)

> Historical planning record. The frontend is now a Vue 3 application; this page
> explains the intended vertical slices and release gates behind that work.

## Recorded Architecture

The plan defines a Vue shell with router/session/project context, then vertical
slices for Geometry, Reaction and MappedReaction, calculations/artifacts/search,
upload queue, account/project administration, and E2E release gates. It calls
for a shared API contract, visible authorization state, project-scoped views,
and removal of obsolete entry points.

## Current UX Contract

Current catalog pages expose explicit sort controls, query-in-progress state,
and adjacent-page prefetch. Geometry detail displays charge, multiplicity,
coordinates, XYZ/SDF downloads, and can close from the backdrop. Upload lists
show `pending`, `partial`, `filtered`, and failed ingestion outcomes distinctly.
ChemDoodle must not infer implicit hydrogens that are absent from the stored
MolGR graph. Verify changes with `make frontend-check`, `make frontend-build`,
and relevant Playwright tests.
