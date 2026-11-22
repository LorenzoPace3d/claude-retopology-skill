"""
Retopology helper library for the /retopology skill.

Load inside Blender (via the Blender MCP execute tool) with:
    exec(open(r"<skill dir>/scripts/retopo_lib.py", encoding="utf-8").read(), globals())

Every public function returns a plain dict/list so results can be read back.
Nothing here deletes or modifies the ORIGINAL high-poly objects: all work
happens on new objects (the _LP copy and a temporary hidden proxy).
"""
import bpy
import bmesh
import math
import re
from mathutils import Vector
from mathutils.bvhtree import BVHTree

PROXY_SUFFIX = "__retopo_proxy"
LP_RE = re.compile(r"([_\-. ])?(HighPoly|highpoly|HIGHPOLY|High|high|HIGH|HP|hp|Hp|Hi|hi|HI)$")
_LP_MAP = {
    "HighPoly": "LowPoly", "highpoly": "lowpoly", "HIGHPOLY": "LOWPOLY",
    "High": "Low", "high": "low", "HIGH": "LOW",
    "HP": "LP", "hp": "lp", "Hp": "Lp",
    "Hi": "Lo", "hi": "lo", "HI": "LO",
}
LP_DETECT = re.compile(r"([_\-. ])(LP|lp|Low|low|LOW|LowPoly|lowpoly)(\.\d{3})?$")


# --------------------------------------------------------------------------- helpers
def lp_name(name):
    """Rock_01 -> Rock_01_LP ; Rock_01_HP -> Rock_01_LP ; Rock-High -> Rock-Low.
    A trailing Blender duplicate number (.001) is kept after the suffix."""
    num = ""
    m = re.search(r"(\.\d{3})$", name)
    if m:
        num, name = m.group(1), name[: -len(m.group(1))]
    m = LP_RE.search(name)
    if m and m.group(1):  # only treat it as a suffix when separated (_HP, -High, .hp)
        sep = m.group(1)
        base = name[: m.start()]
        return f"{base}{sep}{_LP_MAP[m.group(2)]}{num}"
    return f"{name}_LP{num}"


def _world_bm(obj, local=False):
    dg = bpy.context.evaluated_depsgraph_get()
    eo = obj.evaluated_get(dg)
    me = eo.to_mesh()
    bm = bmesh.new()
    bm.from_mesh(me)
    if not local:
        bm.transform(obj.matrix_world)
    eo.to_mesh_clear()
    return bm


def _tri_count(bm):
    return sum(len(f.verts) - 2 for f in bm.faces)


def _apply_mods(obj):
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    eo = obj.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(eo)
    old = obj.data
    obj.modifiers.clear()
    obj.data = me
    me.name = obj.name
    if old.users == 0:
        bpy.data.meshes.remove(old)


def _islands(bm):
    seen, count = set(), 0
    for v in bm.verts:
        if v.index in seen:
            continue
        count += 1
        stack = [v]
        seen.add(v.index)
        while stack:
            cur = stack.pop()
            for e in cur.link_edges:
                o = e.other_vert(cur)
                if o.index not in seen:
                    seen.add(o.index)
                    stack.append(o)
    return count


def _diag(obj):
    return Vector(obj.dimensions).length


def _edge_len_for(area, tris):
    # edge length of an equilateral triangle when `area` is split into `tris` triangles
    return math.sqrt(4.0 * (area / max(tris, 1)) / math.sqrt(3.0))


def _signed_volume(bm):
    vol = 0.0
    for f in bm.faces:
        vs = [v.co for v in f.verts]
        for i in range(1, len(vs) - 1):
            vol += vs[0].dot(vs[i].cross(vs[i + 1])) / 6.0
    return vol


# --------------------------------------------------------------------------- 1. analyze
def analyze(names=None):
    """Return stats for each mesh object (or only `names`). Skips _LP objects and proxies."""
    scene = bpy.context.scene
    if names:
        objs = [bpy.data.objects[n] for n in names]
    else:
        objs = [o for o in scene.objects if o.type == "MESH"
                and PROXY_SUFFIX not in o.name and not LP_DETECT.search(o.name)]
    us = scene.unit_settings
    out = []
    for o in objs:
        bm = _world_bm(o)
        bm.verts.index_update()
        area = sum(f.calc_area() for f in bm.faces)
        tris = _tri_count(bm)
        out.append({
            "name": o.name,
            "lp_name": lp_name(o.name),
            "lp_exists": lp_name(o.name) in bpy.data.objects,
            "tris": tris,
            "verts": len(bm.verts),
            "dimensions_m": [round(d * us.scale_length, 4) for d in o.dimensions],
            "bbox_diagonal_m": round(_diag(o) * us.scale_length, 4),
            "surface_area_m2": round(area * us.scale_length ** 2, 4),
            "loose_parts": _islands(bm),
            "boundary_edges": sum(1 for e in bm.edges if e.is_boundary),
            "non_manifold_edges": sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary),
            "unapplied_scale": [round(s, 4) for s in o.scale] if any(abs(s - 1) > 1e-4 for s in o.scale) else None,
            "modifiers": [m.type for m in o.modifiers],
            "materials": [s.material.name for s in o.material_slots if s.material],
        })
        bm.free()
    return {"unit_scale": us.scale_length, "length_unit": us.length_unit, "objects": out}


# --------------------------------------------------------------------------- 2. proxy (crack closing)
def make_proxy(src_name, target_tris, voxel_size=None, close_radius=None, smooth_iterations=0):
    """Build a hidden, crack-free copy of the source used as the decimation base.

    Crack closing is a morphological CLOSING: the surface is pushed out by close_radius,
    voxel-remeshed (walls of narrow cracks/dents overlap and fuse), then pulled back in
    by the same amount and remeshed again. Cracks, fissures and dents narrower than about
    2 x close_radius disappear completely instead of leaving half-closed grooves and pits;
    convex forms and the silhouette come back to where they were.

    close_radius: default = 1.0 x the expected low-poly edge length (fills cracks, slots
    and dents narrower than ~2 LP edges; 0.5 x left visible grooves on a real cliff).
    Raise to 1.5 x if crack remnants/pits survive; lower if real notches that shape the
    silhouette get filled. 0 disables.
    voxel_size: default = 1/4 of the expected LP edge length.
    smooth_iterations: 0 by default; smoothing rounds off sharp ridges (tested)."""
    src = bpy.data.objects[src_name]
    bm = _world_bm(src)
    area = sum(f.calc_area() for f in bm.faces)
    parts_before = _islands(bm)
    bm.free()

    lp_edge = _edge_len_for(area, target_tris)
    if voxel_size is None:
        voxel_size = lp_edge / 4.0
    # keep the proxy below ~3M tris so Blender stays responsive
    voxel_size = max(voxel_size, math.sqrt(2.0 * area / 3.0e6))

    pname = src.name + PROXY_SUFFIX
    if pname in bpy.data.objects:
        old = bpy.data.objects[pname]
        bpy.data.objects.remove(old, do_unlink=True)

    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(src.evaluated_get(dg))
    proxy = bpy.data.objects.new(pname, me)
    proxy.matrix_world = src.matrix_world.copy()
    for c in src.users_collection:
        c.objects.link(proxy)
    # bake scale/rotation into the mesh so voxel size is in world units
    proxy.data.transform(proxy.matrix_world)
    proxy.matrix_world.identity()

    if close_radius is None:
        close_radius = 1.0 * lp_edge
    close_radius = max(close_radius, 0.0)

    def remesh():
        r = proxy.modifiers.new("Remesh", "REMESH")
        r.mode = "VOXEL"
        r.voxel_size = voxel_size
        r.adaptivity = 0.0

    def offset(dist):
        d = proxy.modifiers.new("Offset", "DISPLACE")
        d.direction = "NORMAL"
        d.mid_level = 0.0
        d.strength = dist

    remesh()
    if close_radius > 0:
        offset(close_radius)    # dilate: crack walls overlap
        remesh()                # fuse
        offset(-close_radius)   # erode back to the original surface
        remesh()
    if smooth_iterations:
        s = proxy.modifiers.new("Smooth", "SMOOTH")
        s.factor = 0.5
        s.iterations = int(smooth_iterations)
    _apply_mods(proxy)

    pbm = bmesh.new()
    pbm.from_mesh(proxy.data)
    pbm.verts.index_update()
    parts_after = _islands(pbm)
    ptris = _tri_count(pbm)
    pbm.free()
    proxy.hide_render = True
    try:
        proxy.hide_set(True)  # keep the viewport readable; make_lp only needs its data
    except RuntimeError:
        pass
    return {
        "proxy": proxy.name,
        "voxel_size": voxel_size,
        "close_radius": close_radius,
        "expected_lp_edge_length": lp_edge,
        "proxy_tris": ptris,
        "loose_parts_before": parts_before,
        "loose_parts_after": parts_after,
        "warning": ("loose part count changed: thin pieces may have vanished or separate "
                    "pieces fused" if parts_after != parts_before else None),
    }


# --------------------------------------------------------------------------- 3. low poly
def _collapse_to(obj, target_tris, tries=4):
    for _ in range(tries):
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        tris = _tri_count(bm)
        bm.free()
        if tris <= target_tris * 1.03:
            return tris
        d = obj.modifiers.new("Decimate", "DECIMATE")
        d.decimate_type = "COLLAPSE"
        d.ratio = max(min(target_tris / tris, 1.0), 1e-4)
        d.use_collapse_triangulate = True
        _apply_mods(obj)
    return len(obj.data.polygons)


def _fix_pits(bm, passes=3, pit_min=0.12):
    """Lift single-vertex dents (vertex below its whole ring) to the ring's average."""
    fixed = 0
    for _ in range(passes):
        pits, _ = _dents(bm, pit_min=pit_min, groove_min=1e9)
        if not pits:
            break
        for v in pits:
            nb = [e.other_vert(v) for e in v.link_edges]
            v.co = sum((u.co for u in nb), Vector()) / len(nb)
        fixed += len(pits)
    bm.normal_update()
    return fixed


def _refine(bm, proxy_bvh, budget_tris, max_edge, spread_deg=20.0, size_factor=2.0, iters=8):
    """Spend up to `budget_tris` splitting (1) edges longer than `max_edge` and (2) the
    longest edge of faces whose smooth vertex normals spread more than `spread_deg`
    (streak-prone). New points are snapped onto the crack-free proxy surface, so the
    silhouette only gets better. This confines smooth-shading gradients to narrow bands
    along ridges instead of stretching them across big triangles."""
    start = _tri_count(bm)
    lim = math.radians(spread_deg)
    for _ in range(iters):
        room = (budget_tris - (_tri_count(bm) - start)) // 2  # one split = +2 tris
        if room <= 0:
            break
        bm.normal_update()
        med = _median_edge(bm)
        cand = {}
        for f in bm.faces:
            le = max(f.edges, key=lambda e: e.calc_length())
            ln = le.calc_length()
            if ln > max_edge:
                pr = 1e6 + ln
            elif ln > size_factor * med:
                s = _tri_spread(f)
                if s <= lim:
                    continue
                pr = s * ln
            else:
                continue
            cand[le] = max(cand.get(le, 0.0), pr)
        if not cand:
            break
        edges = sorted(cand, key=lambda e: -cand[e])[:room]
        for e in edges:
            if not e.is_valid:
                continue
            # split the edge, then cut each neighbouring triangle from the new point to
            # its opposite corner (never along the old edge: that makes needle triangles)
            faces = [(f, next(v for v in f.verts if v not in e.verts)) for f in e.link_faces
                     if len(f.verts) == 3]
            _, nv = bmesh.utils.edge_split(e, e.verts[0], 0.5)
            hit = proxy_bvh.find_nearest(nv.co)
            if hit[0] is not None:
                nv.co = hit[0]
            for f, opp in faces:
                if f.is_valid and opp.is_valid:
                    try:
                        bmesh.utils.face_split(f, nv, opp)
                    except ValueError:
                        pass
        bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3],
                              quad_method="BEAUTY", ngon_method="BEAUTY")
    return _tri_count(bm) - start


def _beautify_flat(bm, max_deg=4.0):
    """Flip edges between nearly coplanar triangles to remove needle triangles on flat
    areas. Restricted to near-flat edges so the shape does not change."""
    lim = math.radians(max_deg)
    edges = [e for e in bm.edges if e.is_manifold and e.calc_face_angle(0.0) < lim]
    faces = list({f for e in edges for f in e.link_faces})
    if edges:
        bmesh.ops.beautify_fill(bm, faces=faces, edges=edges)
    return len(edges)


def make_lp(src_name, target_tris, proxy_name=None, flat_dissolve_deg=5.0, prepass_factor=1.6,
            refine_reserve=0.2, max_edge_factor=4.0, normals_weight=0):
    """Decimate the proxy into the _LP object (triangulated, smooth shaded, outward normals).
    The _LP gets the same transform, collections and materials as the source.

    flat_dissolve_deg: after a first collapse, faces that are within this angle of each
    other are merged and re-triangulated into large triangles, so flat areas end up
    sparse and the budget goes to silhouette and curvature. 0 disables.
    refine_reserve: fraction of the budget kept back for the shading refinement pass.
    max_edge_factor: no edge longer than this x the expected LP edge (giant fan
    triangles caused visible shading lines on a real cliff).
    normals_weight: 0 = plain smooth normals (default). >0 applies face-area weighted
    custom normals; NOT the default because on real cliffs they turned every concave
    groove into a dark slot."""
    src = bpy.data.objects[src_name]
    proxy = bpy.data.objects[proxy_name or src.name + PROXY_SUFFIX]
    name = lp_name(src.name)
    if name in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)

    me = proxy.data.copy()
    lp = bpy.data.objects.new(name, me)
    for c in src.users_collection:
        c.objects.link(lp)

    pbm = bmesh.new()
    pbm.from_mesh(proxy.data)
    area = sum(f.calc_area() for f in pbm.faces)
    proxy_bvh = BVHTree.FromBMesh(pbm)
    pbm.free()
    lp_edge = _edge_len_for(area, target_tris)
    base_target = int(target_tris * (1.0 - refine_reserve))

    # quadric-error collapse keeps curvature/silhouette; the planar pass thins out flats
    if flat_dissolve_deg and flat_dissolve_deg > 0:
        _collapse_to(lp, int(base_target * prepass_factor))
        bm = bmesh.new()
        bm.from_mesh(lp.data)
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(flat_dissolve_deg),
                                 verts=bm.verts[:], edges=bm.edges[:])
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method="BEAUTY", ngon_method="BEAUTY")
        bm.to_mesh(lp.data)
        bm.free()
    _collapse_to(lp, base_target)

    bm = bmesh.new()
    bm.from_mesh(lp.data)
    pits_fixed = _fix_pits(bm)
    _beautify_flat(bm)
    refined = _refine(bm, proxy_bvh, target_tris - _tri_count(bm), max_edge_factor * lp_edge)
    # leftover budget: a finer pass on milder gradients
    refined += _refine(bm, proxy_bvh, target_tris - _tri_count(bm), max_edge_factor * lp_edge,
                       spread_deg=12.0)
    pits_fixed += _fix_pits(bm)
    _beautify_flat(bm)
    diag = max((Vector(proxy.dimensions)).length, 1e-6)
    bmesh.ops.dissolve_degenerate(bm, dist=diag * 1e-6, edges=bm.edges[:])
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method="BEAUTY", ngon_method="BEAUTY")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    if _signed_volume(bm) < 0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
    # move geometry back into the source's local space so the LP shares its transform
    bm.transform(src.matrix_world.inverted())
    bm.to_mesh(lp.data)
    bm.free()
    lp.matrix_world = src.matrix_world.copy()

    lp.data.materials.clear()
    for slot in src.material_slots:
        lp.data.materials.append(slot.material)
    for p in lp.data.polygons:
        p.use_smooth = True
    lp.data.update()
    if normals_weight:
        apply_weighted_normals(lp.name, normals_weight)
    return {"lp": lp.name, "tris": len(lp.data.polygons), "target": target_tris,
            "tris_added_by_refine": refined, "pits_lifted": pits_fixed,
            "weighted_normals": bool(normals_weight)}


def apply_weighted_normals(lp_name_, weight=80):
    """Face-area weighted custom normals. Big flat triangles keep (almost) their own flat
    normal, so the smooth-shading gradient is squeezed into the small triangles along
    ridges instead of being stretched across a large face, which is what shows up as a
    light/dark streak with a hard line at its edge. Unreal keeps them when the FBX is
    imported with Normal Import Method = Import Normals (the default); bake the normal
    map on the same exported mesh."""
    obj = bpy.data.objects[lp_name_]
    me = obj.data
    if hasattr(me, "use_auto_smooth"):  # Blender < 4.1
        me.use_auto_smooth = True
    for p in me.polygons:
        p.use_smooth = True
    m = obj.modifiers.new("WeightedNormal", "WEIGHTED_NORMAL")
    m.mode = "FACE_AREA"
    m.weight = int(weight)
    m.keep_sharp = False
    _apply_mods(obj)
    return {"custom_normals": obj.data.has_custom_normals}


# --------------------------------------------------------------------------- shading / dent metrics
def _median_edge(bm):
    lens = sorted(e.calc_length() for e in bm.edges)
    return lens[len(lens) // 2] if lens else 0.0


def _tri_spread(f):
    """Largest angle (rad) between the smooth vertex normals of a face. With smooth
    shading the face interpolates between these normals: a big spread on a big face is a
    visible light/dark streak, ending in a hard line where the next face starts."""
    ns = [v.normal for v in f.verts]
    best = 0.0
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            best = max(best, ns[i].angle(ns[j], 0.0))
    return best


def _streak_faces(bm, spread_deg=20.0, size_factor=2.0):
    """Faces that will show a smooth-shading streak: normal spread above `spread_deg`
    AND longest edge above `size_factor` x median edge (small faces may bend freely)."""
    bm.normal_update()
    med = _median_edge(bm)
    lim = math.radians(spread_deg)
    out = []
    for f in bm.faces:
        longest = max(e.calc_length() for e in f.edges)
        if longest > size_factor * med:
            s = _tri_spread(f)
            if s > lim:
                out.append((s * longest, f))
    out.sort(key=lambda x: -x[0])
    return [f for _, f in out]


def _corner_normals(me):
    if hasattr(me, "corner_normals"):  # Blender 4.1+
        return [c.vector.copy() for c in me.corner_normals]
    me.calc_normals_split()
    return [l.normal.copy() for l in me.loops]


def _streaks_mesh(obj, spread_deg=20.0, size_factor=2.0):
    """Streak-prone faces measured on the normals that will actually be exported
    (custom/weighted normals included). Returns (count, worst world positions)."""
    me = obj.data
    cn = _corner_normals(me)
    co = [v.co for v in me.vertices]
    lens = sorted((co[a] - co[b]).length for a, b in (e.vertices for e in me.edges))
    med = lens[len(lens) // 2] if lens else 0.0
    lim = math.radians(spread_deg)
    out = []
    for p in me.polygons:
        vs = p.vertices
        n = len(vs)
        longest = max((co[vs[i]] - co[vs[(i + 1) % n]]).length for i in range(n))
        if longest <= size_factor * med:
            continue
        ns = [cn[li] for li in p.loop_indices]
        s = max(ns[i].angle(ns[j], 0.0) for i in range(n) for j in range(i + 1, n))
        if s > lim:
            out.append((s * longest, obj.matrix_world @ p.center))
    out.sort(key=lambda x: -x[0])
    return len(out), [[round(c, 3) for c in p] for _, p in out[:5]]


def _concavity(v):
    """How far a vertex sits BELOW its ring of neighbours, along its normal, relative to
    the ring's edge length. >0 = concave. Returns (value, is_pit)."""
    nb = [e.other_vert(v) for e in v.link_edges]
    if len(nb) < 3:
        return 0.0, False
    avg = sum((u.co for u in nb), Vector()) / len(nb)
    ring = sum((u.co - v.co).length for u in nb) / len(nb)
    if ring <= 0:
        return 0.0, False
    c = (avg - v.co).dot(v.normal) / ring
    pit = all((u.co - v.co).dot(v.normal) > 0 for u in nb)
    return c, pit


def _dents(bm, pit_min=0.12, groove_min=0.35):
    """Small concave defects: pits (every neighbour above the vertex) and sharp concave
    vertices (crack remnants). Real wide concave forms are smooth at LP scale and stay
    under these thresholds."""
    bm.normal_update()
    pits, grooves = [], []
    for v in bm.verts:
        c, pit = _concavity(v)
        if pit and c > pit_min:
            pits.append((c, v))
        elif c > groove_min:
            grooves.append((c, v))
    pits.sort(key=lambda x: -x[0])
    grooves.sort(key=lambda x: -x[0])
    return [v for _, v in pits], [v for _, v in grooves]


# --------------------------------------------------------------------------- 4. checks
def _signed(bvh, co):
    """Signed distance from `co` to the surface in `bvh`: + outside, - inside."""
    loc, nrm, _, dist = bvh.find_nearest(co)
    if loc is None:
        return None
    return dist if (co - loc).dot(nrm) > 0 else -dist


def _stats(vals, pct):
    vals = sorted(vals)
    if not vals:
        return {"mean": 0.0, "p95": 0.0, "max": 0.0}
    return {"mean": pct(sum(vals) / len(vals)), "p95": pct(vals[int(len(vals) * 0.95)]),
            "max": pct(vals[-1])}


def _deviation(lp, hp, max_samples=60000):
    """Compare the low poly against the ORIGINAL high poly.

    silhouette_loss: HP surface sticking OUT of the LP (ridges/bumps cut off). This is
                     what shows on the outline, keep it small.
    lp_bulge:        LP sticking OUT of the HP (puffed up areas). Also visible, keep small.
    filled_detail:   HP surface INSIDE the LP (cracks, pits closed over). Expected and
                     wanted: the normal map carries it. Informational only."""
    lbm = _world_bm(lp)
    hbm = _world_bm(hp)
    lbm.normal_update()
    hbm.normal_update()
    lbvh = BVHTree.FromBMesh(lbm)
    hbvh = BVHTree.FromBMesh(hbm)
    diag = max(_diag(hp), 1e-6)
    pct = lambda x: round(100.0 * x / diag, 3)

    out_, in_, worst = [], [], []
    step = max(1, len(hbm.verts) // max_samples)
    for i, v in enumerate(hbm.verts):
        if i % step:
            continue
        s = _signed(lbvh, v.co)
        if s is None:
            continue
        if s > 0:
            out_.append(s)
            worst.append((s, v.co.copy()))
        else:
            in_.append(-s)
    bulge = [s for s in (_signed(hbvh, v.co) for v in lbm.verts) if s and s > 0]
    lbm.free()
    hbm.free()
    worst = sorted(worst, key=lambda x: -x[0])[:5]
    return {
        "silhouette_loss_pct_of_diag": _stats(out_, pct),
        "lp_bulge_pct_of_diag": _stats(bulge, pct),
        "filled_detail_max_depth_pct_of_diag": _stats(in_, pct)["max"],
        "worst_silhouette_points_world": [[round(c, 4) for c in p[1]] for p in worst],
    }


def check(lp_name_, src_name=None, smooth_angle_warn_deg=80.0, sliver_deg=4.0):
    """Topology + shading report for the low poly, plus silhouette deviation against the
    original high poly `src_name` (when given)."""
    lp = bpy.data.objects[lp_name_]
    bm = _world_bm(lp)
    diag = max(_diag(lp), 1e-6)
    eps_area = (diag * 1e-5) ** 2
    non_tris = sum(1 for f in bm.faces if len(f.verts) != 3)
    degenerate = sum(1 for f in bm.faces if f.calc_area() < eps_area)
    slivers, sliver_pts = 0, []
    for f in bm.faces:
        if len(f.loops) == 3 and min(l.calc_angle() for l in f.loops) < math.radians(sliver_deg):
            slivers += 1
            if len(sliver_pts) < 5:
                sliver_pts.append([round(c, 4) for c in f.calc_center_median()])
    harsh, harsh_pts = 0, []
    for e in bm.edges:
        if e.is_manifold:
            a = math.degrees(e.calc_face_angle(0.0))
            if a > smooth_angle_warn_deg:
                harsh += 1
                if len(harsh_pts) < 5:
                    harsh_pts.append([round(c, 4) for c in ((e.verts[0].co + e.verts[1].co) / 2)])
    rep = {
        "tris": len(bm.faces),
        "non_triangles": non_tris,
        "non_manifold_edges": sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary),
        "boundary_edges (holes)": sum(1 for e in bm.edges if e.is_boundary),
        "degenerate_faces": degenerate,
        "sliver_tris (min angle < %g deg)" % sliver_deg: slivers,
        "sliver_locations_world": sliver_pts,
        "normals_outward": _signed_volume(bm) > 0,
        "harsh_smooth_edges (> %g deg, likely shading gradients)" % smooth_angle_warn_deg: harsh,
        "harsh_edge_locations_world": harsh_pts,
    }
    pits, grooves = _dents(bm)
    rep["dent_pits"] = len(pits)
    rep["dent_sharp_concave_verts"] = len(grooves)
    rep["dent_locations_world"] = [[round(c, 3) for c in v.co] for v in (pits + grooves)[:5]]
    bm.free()
    n, pts = _streaks_mesh(lp)
    rep["custom_normals"] = lp.data.has_custom_normals
    rep["shading_streak_faces"] = n
    rep["streak_locations_world"] = pts
    if src_name:
        rep["deviation"] = _deviation(lp, bpy.data.objects[src_name])
    return rep


# --------------------------------------------------------------------------- 5. merge close verts
def merge_close(lp_name_, src_name, dist=None, max_p95_increase_pct=0.15, max_tries=4):
    """Merge vertices closer than `dist` (default: 20% of the median edge length).
    Each attempt is validated against the original high poly `src_name`: no new
    holes/non-manifold/degenerate faces, normals still outward, and silhouette-loss p95
    must not grow by more than `max_p95_increase_pct` (percent of bbox diagonal).
    If an attempt fails, dist is halved and retried."""
    lp = bpy.data.objects[lp_name_]
    before = check(lp_name_, src_name)
    had_custom = lp.data.has_custom_normals
    original = lp.data.copy()

    bm = bmesh.new()
    bm.from_mesh(lp.data)
    lens = sorted(e.calc_length() for e in bm.edges)
    bm.free()
    if dist is None:
        dist = 0.2 * lens[len(lens) // 2]

    keys = ["non_manifold_edges", "boundary_edges (holes)", "degenerate_faces"]
    attempts = []
    for _ in range(max_tries):
        bm = bmesh.new()
        bm.from_mesh(original)
        nv = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=dist)
        bmesh.ops.dissolve_degenerate(bm, dist=dist * 1e-3, edges=bm.edges[:])
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method="BEAUTY", ngon_method="BEAUTY")
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        merged = nv - len(bm.verts)
        bm.to_mesh(lp.data)
        bm.free()
        for p in lp.data.polygons:
            p.use_smooth = True
        lp.data.update()
        if had_custom:
            apply_weighted_normals(lp_name_)
        after = check(lp_name_, src_name)
        grow = (after["deviation"]["silhouette_loss_pct_of_diag"]["p95"]
                - before["deviation"]["silhouette_loss_pct_of_diag"]["p95"])
        ok = (all(after[k] <= before[k] for k in keys) and after["normals_outward"]
              and grow <= max_p95_increase_pct
              and after["shading_streak_faces"] <= before["shading_streak_faces"] + 2
              and after["dent_pits"] <= before["dent_pits"])
        attempts.append({"dist": dist, "merged_verts": merged, "p95_growth_pct": round(grow, 3), "accepted": ok})
        if ok:
            bpy.data.meshes.remove(original)
            return {"accepted": True, "attempts": attempts, "tris_before": before["tris"], "tris_after": after["tris"]}
        dist /= 2.0
    # nothing acceptable: restore
    old = lp.data
    lp.data = original
    original.name = lp.name
    bpy.data.meshes.remove(old)
    return {"accepted": False, "attempts": attempts, "note": "all merge attempts rejected; mesh restored"}


# --------------------------------------------------------------------------- 6. final validation
# Hard errors must be 0 when the skill hands the mesh over. Soft issues are reported.
HARD_ERRORS = [
    "non_triangles", "non_manifold_edges", "holes_boundary_edges", "non_manifold_verts",
    "loose_verts", "loose_edges", "zero_length_edges", "degenerate_faces",
    "inconsistent_winding", "duplicate_faces", "fold_overs", "self_intersections",
]
SOFT_ISSUES = ["slivers", "high_valence_verts", "tiny_faces"]


def _mesh_errors(bm, med, fold_deg=150.0, concave_fold_deg=120.0, sliver_deg=4.0, max_valence=12):
    """Every structural defect we know how to detect, as lists of BMesh elements."""
    bm.normal_update()
    bm.verts.index_update()
    bm.faces.index_update()
    bm.faces.ensure_lookup_table()
    eps = med * 1e-4
    err = {
        "non_triangles": [f for f in bm.faces if len(f.verts) != 3],
        "non_manifold_edges": [e for e in bm.edges if not e.is_manifold and not e.is_boundary and e.link_faces],
        "holes_boundary_edges": [e for e in bm.edges if e.is_boundary],
        "non_manifold_verts": [v for v in bm.verts if v.link_faces and not v.is_manifold],
        "loose_verts": [v for v in bm.verts if not v.link_faces],
        "loose_edges": [e for e in bm.edges if not e.link_faces],
        "zero_length_edges": [e for e in bm.edges if e.calc_length() < eps],
        "degenerate_faces": [f for f in bm.faces if f.calc_area() < eps * eps],
        "inconsistent_winding": [e for e in bm.edges if e.is_manifold and not e.is_contiguous],
        # faces folded back onto each other: spikes, and inward creases that read as a
        # black notch on a smooth face (seen on a real cliff at ~160 deg; inward > 120
        # deg already shades as a black slit)
        "fold_overs": [e for e in bm.edges if e.is_manifold and (
                       e.calc_face_angle(0.0) > math.radians(fold_deg)
                       or (not e.is_convex and e.calc_face_angle(0.0) > math.radians(concave_fold_deg)))],
    }
    seen, dups = set(), []
    for f in bm.faces:
        key = tuple(sorted(v.index for v in f.verts))
        if key in seen:
            dups.append(f)
        seen.add(key)
    err["duplicate_faces"] = dups
    tree = BVHTree.FromBMesh(bm)
    bad = set()
    for i, j in tree.overlap(tree):
        if i >= j:
            continue
        fi, fj = bm.faces[i], bm.faces[j]
        if set(fi.verts) & set(fj.verts):
            continue  # neighbours touch by definition
        bad.add(fi)
        bad.add(fj)
    err["self_intersections"] = list(bad)
    lim = math.radians(sliver_deg)
    err["slivers"] = [f for f in bm.faces if len(f.loops) == 3
                      and min(l.calc_angle() for l in f.loops) < lim]
    err["high_valence_verts"] = [v for v in bm.verts if len(v.link_edges) > max_valence]
    areas = sorted(f.calc_area() for f in bm.faces)
    med_area = areas[len(areas) // 2] if areas else 0.0
    err["tiny_faces"] = [f for f in bm.faces if f.calc_area() < med_area * 0.01]
    return err


def _counts(err):
    return {k: len(v) for k, v in err.items()}


def _worse(c0, c1, extra_key=None):
    if any(c1[k] > c0[k] for k in HARD_ERRORS):
        return True
    return extra_key is not None and c1[extra_key] >= c0[extra_key]


def _cleanup(bm, med):
    eps = med * 1e-4
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    le = [e for e in bm.edges if not e.link_faces]
    if le:
        bmesh.ops.delete(bm, geom=le, context="EDGES")
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=eps)
    bmesh.ops.dissolve_degenerate(bm, dist=eps, edges=bm.edges[:])
    bm.verts.index_update()
    seen, dups = set(), []
    for f in bm.faces:
        key = tuple(sorted(v.index for v in f.verts))
        if key in seen:
            dups.append(f)
        seen.add(key)
    if dups:
        bmesh.ops.delete(bm, geom=dups, context="FACES_ONLY")
    bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3],
                          quad_method="BEAUTY", ngon_method="BEAUTY")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    if _signed_volume(bm) < 0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces[:])


def _relax_bad(bm, med, iterations=3, factor=0.5):
    """Untangle folds and self-intersections by relaxing the vertices involved (and
    their ring) towards their neighbours. Purely local."""
    err = _mesh_errors(bm, med)
    vs = set()
    for e in err["fold_overs"]:
        vs.update(e.verts)
    for f in err["self_intersections"]:
        vs.update(f.verts)
    ring = set()
    for v in vs:
        ring.update(e.other_vert(v) for e in v.link_edges)
    vs |= ring
    for _ in range(iterations):
        new = {}
        for v in vs:
            nb = [e.other_vert(v) for e in v.link_edges]
            if nb:
                avg = sum((u.co for u in nb), Vector()) / len(nb)
                new[v] = v.co.lerp(avg, factor)
        for v, co in new.items():
            v.co = co
    return len(vs)


def _collapse_needles(bm, med, batch=80):
    """Needle triangles (one very short edge): collapse that edge."""
    err = _mesh_errors(bm, med)
    used, edges = set(), []
    for f in err["slivers"]:
        es = sorted(f.edges, key=lambda e: e.calc_length())
        e0, e2 = es[0], es[-1]
        if e0.calc_length() < 0.35 * e2.calc_length() and not (set(e0.verts) & used):
            edges.append(e0)
            used.update(e0.verts)
            for v in e0.verts:
                used.update(e.other_vert(v) for e in v.link_edges)
        if len(edges) >= batch:
            break
    if edges:
        bmesh.ops.collapse(bm, edges=edges, uvs=False)
    return len(edges)


def _flip_caps(bm, med, max_dihedral_deg=20.0, batch=80):
    """Cap triangles (one angle near 180 deg): flip their longest edge when the two
    faces are nearly coplanar, so the shape does not change."""
    err = _mesh_errors(bm, med)
    lim = math.radians(max_dihedral_deg)
    edges = set()
    for f in err["slivers"]:
        e2 = max(f.edges, key=lambda e: e.calc_length())
        if e2.is_manifold and e2.calc_face_angle(0.0) < lim:
            edges.add(e2)
        if len(edges) >= batch:
            break
    if edges:
        bmesh.ops.rotate_edges(bm, edges=list(edges), use_ccw=False)
    return len(edges)


def _stage(bm, med, fn, log, name, extra_key=None):
    """Run a fix on a copy; keep it only if no hard error got worse (and, if given,
    `extra_key` actually went down)."""
    trial = bm.copy()
    c0 = _counts(_mesh_errors(trial, med))
    n = fn(trial, med)
    c1 = _counts(_mesh_errors(trial, med))
    if n and not _worse(c0, c1, extra_key):
        bm.free()
        log.append({"step": name, "touched": n, "kept": True})
        return trial, True
    trial.free()
    log.append({"step": name, "touched": n, "kept": False})
    return bm, False


def validate(lp_name_, src_name=None, fix=True, rounds=6):
    """FINAL PASS. Detect every mesh error, auto-fix what can be fixed safely, re-check,
    and report. The mesh is only written back when no hard error got worse.

    Hard errors (must end at 0): non-triangles, non-manifold edges/verts, holes, loose
    geometry, zero-length edges, degenerate/duplicate faces, inconsistent winding,
    fold-overs (faces folded back > 150 deg) and self-intersections.
    Soft issues (reported, reduced where safe): slivers, high-valence verts, tiny faces.
    Also checks asset hygiene: smooth shading, no custom normals, no modifiers, same
    transform and material slots as the source, normals outward."""
    lp = bpy.data.objects[lp_name_]
    dev_before = _deviation(lp, bpy.data.objects[src_name]) if src_name else None
    backup = lp.data.copy() if fix else None
    bm = bmesh.new()
    bm.from_mesh(lp.data)
    med = _median_edge(bm)
    before = _counts(_mesh_errors(bm, med))
    log = []
    if fix:
        bm, _ = _stage(bm, med, lambda b, m: (_cleanup(b, m) or 1), log, "cleanup")
        for _ in range(rounds):
            c = _counts(_mesh_errors(bm, med))
            if not c["fold_overs"] and not c["self_intersections"]:
                break
            bm, kept = _stage(bm, med, _relax_bad, log, "relax folds/intersections")
            if not kept:
                break
        bm, _ = _stage(bm, med, lambda b, m: _fix_pits(b), log, "lift pits")
        for fn, name in ((_flip_caps, "flip cap slivers"), (_collapse_needles, "collapse needle slivers")):
            for _ in range(rounds):
                bm, kept = _stage(bm, med, fn, log, name, extra_key="slivers")
                if not kept:
                    break
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        if _signed_volume(bm) < 0:
            bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
    errs = _mesh_errors(bm, med)
    after = _counts(errs)
    where = {k: [[round(c, 3) for c in (lp.matrix_world @ (x.calc_center_median() if hasattr(x, "calc_center_median")
                                                          else (x.co if hasattr(x, "co") else (x.verts[0].co + x.verts[1].co) / 2)))]
                 for x in errs[k][:3]]
             for k in HARD_ERRORS if errs[k]}
    outward = _signed_volume(bm) > 0
    if fix:
        bm.to_mesh(lp.data)
        for p in lp.data.polygons:
            p.use_smooth = True
        lp.data.update()
    bm.free()

    hygiene = {
        "normals_outward": outward,
        "all_faces_smooth": all(p.use_smooth for p in lp.data.polygons),
        "custom_normals": lp.data.has_custom_normals,
        "modifiers": [m.type for m in lp.modifiers],
    }
    if src_name:
        src = bpy.data.objects[src_name]
        hygiene["same_transform_as_source"] = all(
            abs(a - b) < 1e-5 for ra, rb in zip(lp.matrix_world, src.matrix_world) for a, b in zip(ra, rb))
        hygiene["same_materials_as_source"] = ([s.material for s in lp.material_slots]
                                               == [s.material for s in src.material_slots])
    hard_left = {k: after[k] for k in HARD_ERRORS if after[k]}
    wbm = _world_bm(lp)
    pits, grooves = _dents(wbm)
    wbm.free()
    visual = {"shading_streak_faces": _streaks_mesh(lp)[0], "dent_pits": len(pits),
              "dent_sharp_concave_verts": len(grooves)}
    rep = {
        "passed": not hard_left and outward and hygiene["all_faces_smooth"],
        "hard_errors_before": {k: before[k] for k in HARD_ERRORS if before[k]},
        "hard_errors_after": hard_left,
        "hard_error_locations_world": where,
        "soft_before": {k: before[k] for k in SOFT_ISSUES},
        "soft_after": {k: after[k] for k in SOFT_ISSUES},
        "fix_log": log,
        "visual": visual,
        "hygiene": hygiene,
        "tris": len(lp.data.polygons),
    }
    if src_name:
        dev = _deviation(lp, bpy.data.objects[src_name])
        rep["deviation_before"] = dev_before
        rep["deviation"] = dev
        sil_grow = (dev["silhouette_loss_pct_of_diag"]["p95"]
                    - dev_before["silhouette_loss_pct_of_diag"]["p95"])
        bulge_grow = dev["lp_bulge_pct_of_diag"]["max"] - dev_before["lp_bulge_pct_of_diag"]["max"]
        rep["silhouette_p95_change_pct"] = round(sil_grow, 3)
        if fix and (sil_grow > 0.1 or bulge_grow > 0.5):
            # the fixes moved the shape too much: put the mesh back as it was
            old = lp.data
            lp.data = backup
            backup.name = old.name
            bpy.data.meshes.remove(old)
            backup = None
            rep["passed"] = False
            rep["reverted"] = ("fixes changed the silhouette too much (p95 +%.3f, bulge max +%.3f); "
                               "mesh restored, rebuild with make_lp instead" % (sil_grow, bulge_grow))
    if backup is not None:
        bpy.data.meshes.remove(backup)
    return rep


# --------------------------------------------------------------------------- viewport helpers
def _view3d():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return win, area, region, area.spaces.active


def isolate(name, frame=True, wire=False, keep_view=False):
    """Show only `name` in local view (does not change anyone's hide state).
    keep_view=True keeps the current camera angle, to compare HP and LP from the
    exact same view."""
    win, area, region, space = _view3d()
    r3d = space.region_3d
    saved = (r3d.view_rotation.copy(), r3d.view_location.copy(), r3d.view_distance)
    ob = bpy.data.objects[name]
    with bpy.context.temp_override(window=win, area=area, region=region):
        if space.local_view is not None:
            bpy.ops.view3d.localview(frame_selected=False)
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        was_hidden = ob.hide_get()
        if was_hidden:
            ob.hide_set(False)  # local view needs it visible
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        bpy.ops.view3d.localview(frame_selected=frame and not keep_view)
        ob.select_set(False)
    if keep_view:
        r3d.view_rotation, r3d.view_location, r3d.view_distance = saved
    space.shading.type = "SOLID"
    space.overlay.show_wireframes = wire
    return {"isolated": name, "was_hidden_by_user": was_hidden}


def look_at(name, point_world, distance=None, wire=None, tilt=0.3):
    """Aim the viewport at `point_world` on object `name`, looking along the surface
    normal there (slightly tilted so relief is readable). distance defaults to ~10
    LP edges."""
    win, area, region, space = _view3d()
    ob = bpy.data.objects[name]
    dg = bpy.context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(ob, dg)
    p = Vector(point_world)
    loc, n, _, _ = bvh.find_nearest(ob.matrix_world.inverted() @ p)
    nw = (ob.matrix_world.to_3x3() @ n).normalized() if n is not None else Vector((0, -1, 0))
    if distance is None:
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        bm.transform(ob.matrix_world)
        distance = 10 * _median_edge(bm)
        bm.free()
    r3d = space.region_3d
    r3d.view_perspective = "PERSP"
    r3d.view_location = p
    r3d.view_rotation = (nw + Vector((0, 0, tilt))).normalized().to_track_quat("Z", "Y")
    r3d.view_distance = distance
    if wire is not None:
        space.overlay.show_wireframes = wire
    return {"looking_at": list(p), "distance": distance}


def exit_isolate(restore_hidden=None):
    """Leave local view, turn the wireframe overlay off and re-hide the objects the
    user had hidden (pass the names isolate() reported as was_hidden_by_user)."""
    win, area, region, space = _view3d()
    with bpy.context.temp_override(window=win, area=area, region=region):
        if space.local_view is not None:
            bpy.ops.view3d.localview(frame_selected=False)
    space.overlay.show_wireframes = False
    for n in restore_hidden or []:
        if n in bpy.data.objects:
            bpy.data.objects[n].hide_set(True)
    return {"local_view": False}


# --------------------------------------------------------------------------- 7. cleanup
def finalize(src_name):
    """Delete the temporary proxy. Call only when the user is happy with the _LP."""
    pname = src_name + PROXY_SUFFIX
    if pname in bpy.data.objects:
        o = bpy.data.objects[pname]
        me = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if me.users == 0:
            bpy.data.meshes.remove(me)
        return {"removed": pname}
    return {"removed": None}
