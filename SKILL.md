---
name: retopology
description: Game-ready triangulated retopology of organic high-poly meshes (rocks, cliffs, boulders, sculpts, scans) in Blender for Unreal Engine. Analyzes every mesh in the scene, researches and proposes a PS5/high-end triangle budget per object, then builds a "_LP" copy that keeps the silhouette dense, flat areas sparse, closes cracks completely and leaves small surface detail to the normal map. Fixes shading streaks, concave dents, fold-overs and self-intersections, merges close vertices safely, and always ends with a final mesh-validation pass. Use when the user types /retopology (optionally followed by a mesh name or "all"), or asks to retopo, make a low poly, decimate for games, or prepare an organic sculpt or scan for Unreal.
---

# Retopology (organic, triangulated, Unreal-ready)

The goal is a low poly that reads like the high poly from any distance once the
normal map is baked. Every triangle should help the **silhouette** or the
**large forms**. Anything a normal map can fake, such as cracks, pores, chips or
small bumps, gets no geometry. The mesh you hand over must be free of errors:
the user checks it up close.

## Look we're aiming for

These notes come from the user's reference images and feedback:
- The mesh is fully triangulated, with an irregular, organic triangle flow. No quad grids.
- Small triangles sit on the silhouette, ridges, sharp breaks and strongly curved areas.
- Larger triangles cover flat or gently curved faces. Flat areas should be sparser than edges, but without giant fan triangles (see defect 1).
- Cracks, fissures and slots narrower than about two low-poly edges are **completely** closed. Not half-closed, and not left as a shallow groove or pit.
- Large concave forms that shape the object (an arch opening, a big notch between blocks) stay.

## Defects that are never acceptable

The user rejected results for each of these. Look for them every time, up close.

1. **Shading lines and streaks.** A light or dark band across a large face that
   ends in a hard straight line. The cause: a huge triangle touching a ridge
   vertex. With smooth shading, the normal is interpolated across the whole
   triangle, so the gradient is stretched over metres and visibly breaks at the
   next edge. The fix is geometric: no giant triangles, and extra splits where
   vertex normals differ strongly. `make_lp` does this. `check` reports
   `shading_streak_faces`.
2. **Concave dents and crack remnants.** Small dark notches, pits or slot-like
   grooves on surfaces that should read as solid rock, usually left over from a
   crack that was only partly filled. The fix is a morphological closing on the
   proxy, plus lifting single-vertex pits. `make_proxy`, `make_lp` and
   `validate` do this. Reported as `dent_pits` and `dent_sharp_concave_verts`.
3. **Structural mesh errors**, especially **fold-overs** and
   **self-intersections**. A triangle folded back on its neighbour shows as a
   small black notch on an otherwise smooth face. Decimation and vertex moves
   can create them anywhere, without the other checks noticing. The final
   `validate` pass finds and fixes them (step 7).

Don't use weighted or custom normals to hide streaks. Tested on a real cliff,
they turned every concave groove into a dark slot, which is defect 2.
`normals_weight` exists but stays 0 unless the user asks.

## Invocation

`/retopology <ObjectName>` processes one mesh, `/retopology all` (or no
argument) processes every mesh in the scene, and a list of names processes
those meshes. Objects that already look like low polys (`_LP`, `_Low`) and
temporary `__retopo_proxy` objects are skipped.

## Tools

Work through the Blender MCP tools. Blender must be running with the MCP add-on
connected. If it is not, tell the user and stop.

All the geometry work lives in `scripts/retopo_lib.py`, next to this file. It is
deterministic code, so use it rather than writing bpy from scratch.
Start every `execute_blender_code` call by loading it, using this skill's base
directory:

```python
exec(open(r"<SKILL_DIR>/scripts/retopo_lib.py", encoding="utf-8").read(), globals())
```

To read a function's output back, assign a **dict** to a variable named
`result`, for example `result = analyze()`. That is how the Blender MCP execute
tool returns data. A plain string is rejected. Then use
`get_screenshot_of_window_as_image` to look.

Geometry functions (each returns a dict):
| function | what it does |
|---|---|
| `analyze(names=None)` | Per-mesh stats: tris, size in m, surface area, loose parts, holes, non-manifold edges, unapplied scale, and the `_LP` name it will receive. |
| `make_proxy(src, target_tris, voxel_size=None, close_radius=None)` | Makes a hidden, crack-free copy of the source. It voxel-remeshes, pushes the surface out by `close_radius`, remeshes (crack walls fuse), pulls it back in and remeshes again. Defaults: voxel = ¼ of the expected LP edge, close radius = 1× the LP edge, which closes anything narrower than about 2 LP edges. |
| `make_lp(src, target_tris, flat_dissolve_deg=5.0, refine_reserve=0.2, max_edge_factor=4.0)` | Builds `<name>_LP` from the proxy: quadric collapse to 80% of the target, planar pass for sparse flats, lift single-vertex pits, then the last 20% of the budget is spent splitting giant edges (over 4× the LP edge) and streak-prone triangles, with new points snapped onto the proxy surface. Finally it flips edges on flat areas to remove needles. The result is triangulated, with outward normals, smooth shading, and the source's transform, collections and materials. |
| `check(lp_name, src_name)` | **Topology:** non-tris, holes, non-manifold, degenerate faces, slivers, normal direction. **Shading:** `harsh_smooth_edges`, `shading_streak_faces` (measured on the normals that will be exported) and `streak_locations_world`. **Dents:** `dent_pits`, `dent_sharp_concave_verts` and `dent_locations_world`. **Deviation** against the **original**, as % of the bounding-box diagonal: `silhouette_loss` is high poly sticking out of the low poly (cut ridges), `lp_bulge` is low poly outside the high poly (includes filled cracks), and `filled_detail` is crack depth that was closed. |
| `merge_close(lp_name, src_name, dist=None, max_p95_increase_pct=0.15)` | Merges near-coincident vertices. It rolls back any attempt that adds holes, degenerate faces, streaks or pits, or that hurts the silhouette. |
| `validate(lp_name, src_name, fix=True)` | **Mandatory final pass.** It finds every structural error, fixes what it can safely, re-checks and reports. **Hard errors** (must end at 0): non-triangles, non-manifold edges and verts, holes, loose geometry, zero-length edges, degenerate or duplicate faces, inconsistent winding, **fold-overs** (faces folded back more than 150°, or inward creases over 120°, which read as black notches) and **self-intersections**. Fixes: cleanup, local relax of folds and intersections, pit lifting, flipping cap slivers on flats, collapsing needle slivers. A fix stage is kept only if no hard error gets worse. It measures the silhouette before and after, and **restores the mesh** if the fixes moved it too much (`reverted` in the report). It also reports soft issues (slivers, vertices with more than 12 edges, tiny faces), visual metrics (streaks, pits), hygiene (outward normals, all faces smooth, no custom normals, no modifiers, same transform and materials as the source) and `hard_error_locations_world`. `passed` must be true. With `fix=False` it only reports. |
| `finalize(src)` | Deletes the proxy. |
| `lp_name(name)` | `Rock_01`→`Rock_01_LP`, `Rock_01_HP`→`Rock_01_LP`, `Cliff-High`→`Cliff-Low`, `rock_hp`→`rock_lp`. |

Viewport helpers (use these instead of writing view code):
| function | what it does |
|---|---|
| `isolate(name, frame=True, wire=False, keep_view=False)` | Local view with only that object, in solid shading. `keep_view=True` keeps the current camera angle, so you can compare the HP and LP from the same view. If the user had hidden the object, it is shown temporarily and `was_hidden_by_user` comes back `true`. |
| `look_at(name, point_world, distance=None, wire=None)` | Aims at a point on the surface, looking along the normal there with a slight tilt so relief is readable. Default distance is about 10 LP edges. Use it for every location a report gives. |
| `exit_isolate(restore_hidden=[...])` | Leaves local view, turns the wireframe off, and re-hides the objects the user had hidden. |

The original high-poly object is never modified. Don't apply its modifiers and
don't move it. Its visibility is the user's: if you show it to compare, put it
back exactly as it was (`exit_isolate(restore_hidden=...)`).

## Workflow

### 1. Analyze

Run `analyze()` on the requested objects and look at each mesh, using a
viewport screenshot if needed, to decide what it is (pebble, rock, boulder,
cliff, arch, stump…). Its real-world size in metres matters as much as its
shape.

Flag problems before going on: unapplied scale, a scene unit scale other than 1,
objects that are really several separate parts (a 3-vertex fragment is just
junk the remesh removes), or meshes that are already low poly. If the scene
scale is clearly wrong (a 300 m pebble, say), ask before trusting the
size-based budget. A scale of 0.01 from an FBX in centimetres is normal: the
`_LP` inherits the same transform.

### 2. Research and propose budgets, then wait

For each object, pick a **use category**:
- **hero / close-up**: near the camera or the player, or a focal point
- **mid-ground**: regular world prop
- **background / scatter**: far away or instanced many times

Choose the category from size, detail level and the name. If it's ambiguous,
propose one and say why.

Search the web for current triangle budgets of similar assets on PS5 or other
high-end Unreal Engine 5 projects. Useful searches: "UE5 rock asset triangle
count", "PS5 environment prop polycount", Quixel Megascans LOD0 tri counts for
similar rocks, and artist breakdowns on ArtStation or 80.lv. Base the numbers on
what you find, and cite one or two sources as links. Results are mostly
marketplace listings. Say so; don't present them as an official PS5 standard.

If the search turns up little, use these starting points for non-Nanite LOD0:

| object | hero | mid-ground | background/scatter |
|---|---|---|---|
| pebble / small stone (< 0.5 m) | 500–1,500 | 200–600 | 50–200 |
| rock (0.5–2 m) | 2,000–6,000 | 800–2,500 | 300–800 |
| boulder (2–6 m) | 5,000–15,000 | 2,000–6,000 | 800–2,000 |
| cliff / large rock formation (> 6 m) | 15,000–50,000 | 6,000–20,000 | 2,000–6,000 |

Adjust within or beyond the range for silhouette complexity. An arch or a
jagged spire needs more than a smooth egg-shaped boulder.

Present one compact table: object, size, current tris, category, proposed
target, and a one-line reason. Then **stop and wait** for the user to accept or
give their own numbers. Don't create anything before they answer.

### 3. Build the low poly (per object)

1. `make_proxy(src, target)`. If `warning` reports a changed part count, look at
   it: thin pieces such as twigs or an arch bridge may have disappeared. Lower
   `close_radius` or `voxel_size` to fix that.
2. `make_lp(src, target)`, then `check(lp, src)`.
3. **Inspect up close.** A full-object screenshot hides every defect listed
   above: the streak line, the dents and the black notch were all missed that
   way. With `isolate(lp)` and screenshots, take:
   - two or three full views from different sides, compared with the high poly
     from the same angles (`isolate(src, keep_view=True)`);
   - at least three close-ups (about a quarter of the object filling the view)
     of the largest flat faces next to ridges, ideally at a grazing angle to the
     light. Look for straight light or dark lines, banding, and dark notches or
     pits;
   - `look_at` on the `streak_locations_world` and `dent_locations_world`
     positions from `check`;
   - one close-up with `wire=True`, to judge distribution: denser on ridges,
     sparser on flats, no giant fans.

   Compare suspicious spots with the high poly from the same view. A concave
   form that exists clearly in the high poly and is wider than about 2 LP edges
   is real. Anything narrower is a defect.
4. Fix what you find, then rebuild:
   - **Lines or streaks still visible**, or `shading_streak_faces` over about
     0.2% of the tris: rebuild `make_lp` with `max_edge_factor=3` and/or
     `refine_reserve=0.3`. If the budget is the limit, tell the user and propose
     a slightly higher target.
   - **Dents, notches or crack remnants:** rebuild `make_proxy` with
     `close_radius` at 1.5× its previous value (it's in the proxy report), then
     `make_lp`.
   - **Ridges too soft, or real notches filled:** lower `close_radius` to
     0.75×.
   - **Flats too dense:** `flat_dissolve_deg` 8. Faceted curves or many slivers:
     3.
5. What to aim for in `check`:
   - `shading_streak_faces` close to 0 (tested: 2,008 dropped to 28 on a 30k
     cliff). `dent_sharp_concave_verts` 0. `dent_pits` low, and none visible in
     the close-ups.
   - Silhouette loss p95 under about 0.5% of the diagonal and max under about
     1.5%. `lp_bulge` up to about 1% is fine, because filled cracks count there.
   - A handful of harsh smooth edges on genuinely sharp rock edges, or on the
     flat base rim that goes into the ground, is acceptable.
6. `merge_close(lp, src)`. If it rejected every attempt, keep the mesh as is.
   That's a normal outcome.
7. **Final validation pass, always, as the last step that touches the mesh.**
   The other checks don't catch everything: on a real cliff that passed every
   other check, this pass found 5 fold-overs and 2 self-intersecting faces. One
   of them was a clearly visible black notch.
   1. `validate(lp, src, fix=False)`, then `look_at` every position in
      `hard_error_locations_world` and take a screenshot, so you know what the
      errors look like.
   2. `validate(lp, src)` to fix.
   3. `passed` must be `true` and `hard_errors_after` empty. Look again at the
      same positions to confirm the fix by eye.
   4. If it fails or comes back `reverted`, try `validate` once more. If it
      still fails, rebuild with `make_lp` and repeat from step 3, or report the
      error and where it is. Never hand over a mesh with hard errors without
      saying so.
   5. `silhouette_p95_change_pct` should be about 0. Report soft issues and
      visual metrics, even when they don't block.
8. `exit_isolate(restore_hidden=[...])` with the objects reported as
   `was_hidden_by_user`. Leave the proxy in place until the user is satisfied,
   then `finalize(src)`. Delete any test or old copies you created yourself.

Process objects one at a time and report briefly after each one, so the user
can stop you early if something looks wrong.

### 4. Final checklist (all true before you say it's done)

- [ ] `validate` → `passed: true`, 0 hard errors, not `reverted`.
- [ ] Triangulated, normals outward, all faces smooth, no custom normals, no modifiers.
- [ ] Name with `_LP`/`_Low` following the original's syntax, same transform and materials as the original.
- [ ] No streaks or lines, dents, black notches or crack remnants in the close-ups (at least three, plus every position a report gave).
- [ ] Silhouette loss p95 < 0.5%, max < 1.5%.
- [ ] Triangle count within 3% of the agreed target, or the real number reported.
- [ ] Original intact, its visibility as the user left it, local view exited, wireframe off.

### 5. Report

Finish with a short summary per object: `_LP` name, final tris against target,
silhouette p95 and max, streak and dent counts, the result of the final
validation (hard errors found and fixed, soft issues left), how many vertices
were merged, and anything you noticed but left alone. The skill stops here: no
UVs and no baking, unless the user asks. The .blend isn't saved by the skill:
remind the user.

## When things go wrong

- **Huge source (millions of tris):** `make_proxy` caps the proxy at about 3M
  tris on its own. An 8.8M-tri cliff took about 10 s for the proxy, about 7 s
  for the LP and about 10 s for `validate`.
- **Open or non-manifold scan:** the voxel remesh closes holes. Mention this,
  because the base of a scan may now be capped.
- **Decimate overshoots or undershoots:** `make_lp` re-runs the decimate up to
  four times to get within 3% of the target. If it still misses, report the
  real count. `validate` can remove a few dozen triangles when it collapses
  needles. That's normal.
- **A function errors:** read the traceback, fix the call (wrong name, object
  not visible in the view layer), and don't rewrite the library on the fly. If
  the library itself has a bug, tell the user what failed.
