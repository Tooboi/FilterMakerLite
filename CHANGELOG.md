# Changelog

## [0.7.1]

### Added
- **Boolean tool.** Cut the source mesh with a bounding box, an envelope of the pleated form or any object, so the filter sits in its housing. Applied when you click Finish (Bake).
- **UVs unrolled across the folds.** Textures keep their size on every pleat flank and on the frame.
- **Apply Curve Edit** button: rebuilds the filter after you reshape its curve source.

### Changed
- Curve sources are no longer followed by a background handler; use **Apply Curve Edit** (or Update) after editing the curve.
- The add-on is fully self-contained: it holds only its own features and no longer checks for other add-ons.
- Faster mesh assembly for face and curve filters.

## [0.6.6]

- First release of Filter Maker Lite.
- All sources (new line, curve, mesh edges, faces), pleat shaping, pleat bevel, quads / tris, shading and a flat border.
