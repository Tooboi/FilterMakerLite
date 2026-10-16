# Blender Extensions listing: Filter Maker Lite 0.7.0

**Name:** Filter Maker Lite
**Tagline (max 64 chars):** Free pleated filter generator for lines, curves and faces
**Tags:** Modeling, Mesh, Object
**License:** GPL-3.0-or-later
**Website:** https://github.com/Tooboi/FilterMakerLite

## Description

Make pleated air filters in Blender from a line, a curve, mesh edges or faces. Everything lives in one N-panel tab: no node setup and no modifiers to wire up.

**Four ways to start**
- **New Filter Line**: an editable curve at the 3D cursor; reshape it and apply the edit to rebuild the filter.
- **Any curve**, open or closed: rings, S-bends, paths.
- **Mesh edges**: a wire outline becomes a pleated wall.
- **Faces**: neighbouring faces become one continuous pleated sheet that runs around corners.

**Shape the pleats**
Density, depth, offset, pleat angle, surface detail, height and direction, optional thickness, and a ridge / valley bevel from sharp paper folds to soft rounded media.

**Clean results**
- A structured quad grid with welded seams and smooth normals across panel joins (triangles are one click away).
- UVs unrolled across the folds, so textures keep their size.
- A flat border sealed around the pleat ends, so nothing pokes through.

**Set it into your model**
The Boolean tool cuts the source mesh with a box, an envelope of the pleated form or any object, so the filter sits in its housing. Click **Finish (Bake)** to apply it and keep plain meshes.

Sliders rebuild the filter as you drag. Curve sources stay editable: click **Edit**, reshape the curve, then **Apply Curve Edit**. **Finish (Bake)** turns the filter into regular objects.

## What's new in 0.7.0
- Boolean tool: cut the source mesh so the filter sits in its housing.
- UVs unrolled across the folds.
- Apply Curve Edit: reshape a curve source, then rebuild the filter with one click.
- Faster rebuilds.

## Images (docs/listing/)
- `featured_1920x1080.png`: featured image
- `preview_1_sources.png`: Four ways to start
- `preview_2_bevel.png`: Shape every pleat
- `preview_3_topology.png`: Clean quad topology
- `preview_4_boolean.png`: Set it into your model
- `icon_256.png`: icon (unchanged)
