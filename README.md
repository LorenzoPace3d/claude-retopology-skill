# /retopology: a Claude Code skill for game-ready organic low polys in Blender

A [Claude Code](https://claude.com/claude-code) skill that turns high-poly organic
meshes (rocks, cliffs, boulders, sculpts, scans) into **triangulated low polys
ready for Unreal Engine**. Claude drives Blender through the Blender MCP add-on.

```
/retopology all
/retopology SM_Cliff_002_HP
```

## What it does

1. **Analyzes** every mesh in the scene: size in metres, triangle count, loose
   parts, holes, unapplied scale.
2. **Proposes a triangle budget** per object (hero, mid-ground or
   background/scatter), researched online for high-end / PS5 UE5 assets, and
   **waits for your confirmation**.
3. **Builds a `_LP` copy.** The original is never modified. `Rock_HP` becomes
   `Rock_LP`, `Rock-High` becomes `Rock-Low`, and `Rock` becomes `Rock_LP`.
   - Cracks and fissures narrower than about two low-poly edges are **closed
     completely** by a morphological closing on a voxel proxy. That detail
     belongs to the normal map, not the geometry.
   - Quadric decimation keeps the silhouette dense. A planar pass keeps flat
     areas sparse.
   - The last 20% of the budget goes to a **shading refinement pass**. It
     splits giant triangles and the triangles whose vertex normals diverge,
     which would show smooth-shading streaks and lines.
   - It lifts concave pits and merges close vertices, with every step validated
     against the original.
4. **Runs a mandatory final validation pass.** It detects and fixes fold-overs,
   self-intersections, non-manifold geometry, holes, degenerate or duplicate
   faces, inconsistent winding and slivers, then re-measures the silhouette.
   The result is only handed over with 0 hard errors.
5. **Inspects up close.** Claude takes viewport close-ups of flat faces next to
   ridges and of every flagged location, and compares them with the high poly.

Example on a real 8.8M-triangle cliff (13 m tall) at a 30k budget: about 30
seconds in total, silhouette loss p95 0.09% of the bounding-box diagonal,
streak-prone triangles cut from 2,008 to 28, and 0 structural errors after
validation.

## Requirements

- [Claude Code](https://claude.com/claude-code) (CLI or desktop app).
- Blender 4.1+ (developed and tested on **Blender 5.3**).
- The Blender MCP add-on, enabled with its server running, so Claude can reach
  Blender (`execute_blender_code`, viewport screenshots).

## Install

Clone into your personal skills folder. The folder name becomes the command name:

```bash
# macOS / Linux
git clone https://github.com/<user>/<repo>.git ~/.claude/skills/retopology
```

```powershell
# Windows
git clone https://github.com/<user>/<repo>.git "$env:USERPROFILE\.claude\skills\retopology"
```

Restart Claude Code. The skill shows up as `/retopology`. It also triggers on
its own when you ask for something like "make a game-ready low poly of this
rock for Unreal".

## Use

1. Open your scene in Blender and start the MCP server.
2. In Claude Code, run `/retopology all`, `/retopology <ObjectName>`, or list
   several names.
3. Confirm or change the proposed budgets.
4. Review the `_LP` objects. The temporary `__retopo_proxy` objects are deleted
   when you say you're happy.
5. Save your `.blend`. The skill doesn't save it for you.

The skill stops at the low poly: **no UVs and no baking**.

## Files

| file | content |
|---|---|
| `SKILL.md` | The instructions Claude follows: workflow, quality rules, defects that are never acceptable, final checklist. |
| `scripts/retopo_lib.py` | Deterministic Blender Python library: analysis, proxy and crack closing, low poly build, checks, vertex merge, final validation, viewport helpers. Loaded inside Blender by the skill. |

You can also use the library on its own in Blender's Python console:

```python
exec(open(r"/path/to/retopology/scripts/retopo_lib.py", encoding="utf-8").read(), globals())
make_proxy("Rock_HP", 3000); make_lp("Rock_HP", 3000)
merge_close("Rock_LP", "Rock_HP"); print(validate("Rock_LP", "Rock_HP"))
```

## Status

Early version. So far it has been tested on two real cliff meshes (1.2M and
8.8M triangles) and on synthetic rocks. Expect thresholds to need tuning for
other kinds of organic assets. Issues and pull requests are welcome.

The skill runs Python code inside your Blender session. Read
`scripts/retopo_lib.py` if you want to know exactly what it does. It only
creates new objects and never modifies or deletes your originals.

## License

MIT. See [LICENSE](LICENSE).
