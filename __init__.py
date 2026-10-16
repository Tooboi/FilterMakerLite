bl_info = {
    "name": "Filter Maker Lite",
    "author": "Tooboi",
    "version": (0, 7, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Filter Maker",
    "description": "Free pleated filter generator: line, curve, edge and face sources, pleat shaping, a clean border and the Boolean tool.",
    "category": "Object",
}

import bisect
import hashlib
import json
import math
import re
import time
import traceback
from collections import deque

import bpy
import bpy.utils.previews
import bmesh
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Vector, geometry

try:
    import numpy as np
except Exception:                                  # numpy ships with Blender; just in case
    np = None

_busy = [False]
MAX_CUTS = 400
BOOL_NAME = "Filter Boolean"

# Reserved preview state. Kept initialized even when preview creation fails.
_ICONS = [None]
_pcoll = [None]
_pv = {}
_pv_key = {}


def ic(name):
    """Return a supported UI icon for this Blender version, or NONE."""
    if _ICONS[0] is None:
        try:
            _ICONS[0] = set(bpy.types.UILayout.bl_rna.functions['prop'].parameters['icon'].enum_items.keys())
        except Exception:
            _ICONS[0] = set()
    return name if name in _ICONS[0] else 'NONE'


# ---------------------------------------------------------------------------
# UI safety helpers
# ---------------------------------------------------------------------------

def redraw_all():
    try:
        wm = bpy.context.window_manager
        for win in wm.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def safe_draw(fn):
    def wrapper(self, context):
        try:
            fn(self, context)
        except Exception as exc:
            traceback.print_exc()
            try:
                self.layout.label(text="UI error: %s" % exc, icon=ic('ERROR'))
            except Exception:
                pass
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------------------
# Pleat profile
# ---------------------------------------------------------------------------

def _bevel_radii(p):
    return (min(0.25, max(0.0, p.bevel_ridge) * 0.25),
            min(0.25, max(0.0, p.bevel_valley) * 0.25))


def _profile_table(rp, rv, cuts):
    n = max(1, int(cuts))

    def arc(c, p0, p1):
        out = []
        for i in range(n + 1):
            u = i / n
            a, b, d = (1 - u) ** 2, 2 * u * (1 - u), u * u
            out.append((a * p0[0] + b * c[0] + d * p1[0],
                        a * p0[1] + b * c[1] + d * p1[1]))
        return out

    ridge = [(0.0, 1.0)] if rp < 1e-6 else arc((0.0, 1.0), (-rp, 1.0 - 4 * rp), (rp, 1.0 - 4 * rp))
    valley = [(0.5, -1.0)] if rv < 1e-6 else arc((0.5, -1.0), (0.5 - rv, -1.0 + 4 * rv),
                                                 (0.5 + rv, -1.0 + 4 * rv))
    tab = ridge + valley
    out = [tab[0]]
    for q in tab[1:]:
        if q[0] - out[-1][0] > 1e-9:
            out.append(q)
    if len(out) > 1 and abs(out[-1][0] - (out[0][0] + 1.0)) < 1e-9:
        out.pop()
    return out


def _make_eval(table):
    xs, ws = [t[0] for t in table], [t[1] for t in table]
    x0, n = xs[0], len(xs)

    def f(x):
        xm = (x - x0) % 1.0 + x0
        i = max(0, bisect.bisect_right(xs, xm) - 1)
        xa, wa = xs[i], ws[i]
        if i + 1 < n:
            xb, wb = xs[i + 1], ws[i + 1]
        else:
            xb, wb = xs[0] + 1.0, ws[0]
        if xb - xa < 1e-12:
            return wa
        return wa + (wb - wa) * (xm - xa) / (xb - xa)
    return f


def _cycle_xs(table, period_len, depth, maxlen):
    pts = table + [(table[0][0] + 1.0, table[0][1])]
    xs = []
    for (xa, wa), (xb, wb) in zip(pts, pts[1:]):
        if xb - xa < 1e-9:
            continue
        m = 1
        if maxlen > 0:
            seg = math.hypot((xb - xa) * period_len, (wb - wa) * depth)
            m = max(1, int(math.ceil(seg / maxlen - 1e-9)))
        m = min(m, 64)
        for i in range(m):
            xs.append(xa + (xb - xa) * i / m)
    return xs


# ---------------------------------------------------------------------------
# Path and source helpers
# ---------------------------------------------------------------------------

def _polyline_length(pts, closed):
    total = sum((b - a).length for a, b in zip(pts, pts[1:]))
    if closed and len(pts) > 1:
        total += (pts[0] - pts[-1]).length
    return total


def _resample_at(pts, ss, closed):
    P = pts + [pts[0]] if closed else list(pts)
    lens = [0.0]
    for a, b in zip(P, P[1:]):
        lens.append(lens[-1] + (b - a).length)
    total = lens[-1]
    out, seg = [], 0
    for s in ss:
        if total < 1e-9:
            out.append(pts[0].copy())
            continue
        s = min(max(s, 0.0), total)
        while seg < len(P) - 2 and lens[seg + 1] < s:
            seg += 1
        l0, l1 = lens[seg], lens[seg + 1]
        fac = 0.0 if l1 == l0 else (s - l0) / (l1 - l0)
        out.append(P[seg].lerp(P[seg + 1], fac))
    return out


def _resample(pts, n, closed):
    total = _polyline_length(pts, closed)
    ss = [total * i / n if closed else total * i / (n - 1) for i in range(n)]
    return _resample_at(pts, ss, closed)


def _chains(edges):
    adj = {}
    for ei, (a, b) in enumerate(edges):
        if a == b:
            continue
        adj.setdefault(a, []).append(ei)
        adj.setdefault(b, []).append(ei)
    used, res = set(), []

    def walk(start):
        path, v = [start], start
        while True:
            nxt = next((ei for ei in adj[v] if ei not in used), None)
            if nxt is None:
                break
            used.add(nxt)
            a, b = edges[nxt]
            v = b if a == v else a
            path.append(v)
        return path

    for v, es in list(adj.items()):
        if len(es) != 2:
            while any(e not in used for e in adj[v]):
                res.append((walk(v), False))
    for v in list(adj):
        while any(e not in used for e in adj[v]):
            path = walk(v)
            if len(path) > 2 and path[0] == path[-1]:
                res.append((path[:-1], True))
            else:
                res.append((path, False))
    return res


def _r(v):
    return [round(v[0], 5), round(v[1], 5), round(v[2], 5)]


def paths_from_curve(obj):
    mw, paths, closed = obj.matrix_world, [], []
    for sp in obj.data.splines:
        if sp.type == 'BEZIER':
            bp, n = sp.bezier_points, len(sp.bezier_points)
            if n < 2:
                continue
            seg = n if sp.use_cyclic_u else n - 1
            res = max(sp.resolution_u, 8)
            pts = []
            for i in range(seg):
                a, b = bp[i], bp[(i + 1) % n]
                pts += list(geometry.interpolate_bezier(a.co, a.handle_right, b.handle_left, b.co, res))[:-1]
            if not sp.use_cyclic_u:
                pts.append(bp[-1].co.copy())
        else:
            pts = [Vector(pt.co[:3]) for pt in sp.points]
            if len(pts) < 2:
                continue
        paths.append([_r(mw @ point) for point in pts])
        closed.append(bool(sp.use_cyclic_u) and len(pts) > 2)
    return {"paths": paths, "closed": closed}


def paths_from_mesh(obj, selected_only):
    mw = obj.matrix_world
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        edges = [(e.verts[0].index, e.verts[1].index) for e in bm.edges if e.select or not selected_only]
        coords = {v.index: mw @ v.co for v in bm.verts}
    else:
        me = obj.data
        edges = [(e.vertices[0], e.vertices[1]) for e in me.edges]
        coords = {v.index: mw @ v.co for v in me.vertices}
    paths, closed = [], []
    for ids, cl in _chains(edges):
        if len(ids) >= 2:
            paths.append([_r(coords[i]) for i in ids])
            closed.append(cl)
    return {"paths": paths, "closed": closed}


def faces_from_mesh(obj, selected_only):
    mw = obj.matrix_world
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        faces = [[v.index for v in f.verts] for f in bm.faces if f.select or not selected_only]
        coords = {v.index: mw @ v.co for v in bm.verts}
    else:
        me = obj.data
        faces = [list(poly.vertices) for poly in me.polygons]
        coords = {v.index: mw @ v.co for v in me.vertices}
    used = sorted({i for f in faces for i in f})
    remap = {old: new for new, old in enumerate(used)}
    return {"verts": [_r(coords[i]) for i in used], "faces": [[remap[i] for i in f] for f in faces]}


def _src_size(mode, src):
    pts = [p for path in src["paths"] for p in path] if mode == 'CURVE' else src["verts"]
    if not pts:
        return 1.0
    xs, ys, zs = zip(*pts)
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-3)


# ---------------------------------------------------------------------------
# Mesh builders
# ---------------------------------------------------------------------------

def _wall_path(rb, pts, cl, d, H, density):
    L = _polyline_length(pts, cl)
    n = min(2000, max(4, int(round(L * density * 2)))) if cl else min(2000, max(2, int(round(L * density * 2)) + 1))
    rcp = _resample(pts, n, cl)
    rc = [[rb.verts.new(c), rb.verts.new(c + d * H)] for c in rcp]
    for i in range(n if cl else n - 1):
        j = (i + 1) % n
        try:
            rb.faces.new((rc[i][0], rc[j][0], rc[j][1], rc[i][1]))
        except ValueError:
            pass


def _height_dir(p):
    d = Vector(p.height_dir)
    if d.length < 1e-6:
        d = Vector((0, 0, 1))
    d.normalize()
    return d


def _curve_strip_np(bm, q3, wv, uacc, u_end, d, H, rows, cl):
    """Extrude the pleated path into a strip (numpy + foreach_set): vertex (i, k) is
    station i at height row k; UVs = (unrolled length, height) in metres."""
    n = len(q3)
    Q = np.asarray([tuple(q) for q in q3], dtype=np.float64)
    hs = H * np.arange(rows + 1) / rows
    coords = (Q[:, None, :] + np.asarray(tuple(d))[None, None, :] * hs[None, :, None]).reshape(-1, 3)
    ncol = n if cl else n - 1
    i = np.repeat(np.arange(ncol), rows)
    k = np.tile(np.arange(rows), ncol)
    j = (i + 1) % n
    R = rows + 1
    quads = np.stack([i * R + k, j * R + k, j * R + k + 1, i * R + k + 1], axis=1)
    U = np.asarray(uacc, dtype=np.float64)
    ui = U[i]
    uj = np.where(j > i, U[j], u_end)
    v0, v1 = hs[k], hs[k + 1]
    luv = np.stack([np.stack([ui, v0], 1), np.stack([uj, v0], 1),
                    np.stack([uj, v1], 1), np.stack([ui, v1], 1)], axis=1).reshape(-1, 2)
    me = bpy.data.meshes.new(".FM strip tmp")
    try:
        _mesh_from_arrays(me, coords, quads.tolist())
        uvl = me.uv_layers.new(name=UV_LAYER)
        if len(uvl.data) == len(luv):
            uvl.data.foreach_set("uv", luv.astype(np.float32).ravel())
        attr = me.attributes.new(PLEAT_ATTR, 'FLOAT', 'POINT')
        if len(attr.data) == n * R:
            attr.data.foreach_set("value", np.repeat(np.asarray(wv, dtype=np.float32), R))
        me.update()
        bm.from_mesh(me)
    finally:
        bpy.data.meshes.remove(me)


def _curve_strip_py(bm, q3, wv, uacc, u_end, d, H, rows, cl):
    n = len(q3)
    uvl = bm.loops.layers.uv.get(UV_LAYER) or bm.loops.layers.uv.new(UV_LAYER)
    pl = bm.verts.layers.float.get(PLEAT_ATTR) or bm.verts.layers.float.new(PLEAT_ATTR)
    cols = [[bm.verts.new(q3[i] + d * (H * k / rows)) for k in range(rows + 1)] for i in range(n)]
    for i in range(n):
        for vert in cols[i]:
            vert[pl] = wv[i]
    for i in range(n if cl else n - 1):
        j = (i + 1) % n
        ui, uj = uacc[i], (uacc[j] if j > i else u_end)
        for k in range(rows):
            try:
                f = bm.faces.new((cols[i][k], cols[j][k], cols[j][k + 1], cols[i][k + 1]))
            except ValueError:
                continue
            v0, v1 = H * k / rows, H * (k + 1) / rows
            for lp, uv in zip(f.loops, ((ui, v0), (uj, v0), (uj, v1), (ui, v1))):
                lp[uvl].uv = uv


def build_curve(bm, p, src, ref=None):
    d, H = _height_dir(p), max(p.height, 1e-5)
    rows = max(1, min(200, int(round(H * p.detail))))
    table = _profile_table(*_bevel_radii(p), p.bevel_cuts)
    ev, depth = _make_eval(table), p.pleat_depth
    for path, cl in zip(src["paths"], src["closed"]):
        pts = [Vector(x) for x in path]
        L = _polyline_length(pts, cl)
        nhalf = max(2, int(round(L * p.pleat_density * 2)))
        if cl and nhalf % 2:
            nhalf += 1
        Lp = nhalf / 2.0
        period = L / Lp if L > 1e-9 else 1.0 / max(p.pleat_density, 1e-3)
        maxlen = 1.0 / max(p.detail, 1e-3)
        xs = _cycle_xs(table, period, depth, maxlen)
        for _ in range(8):
            if len(xs) * Lp <= 6000:
                break
            maxlen *= 2.0
            xs = _cycle_xs(table, period, depth, maxlen)
        poss = []
        for k in range(-1, int(math.ceil(Lp)) + 2):
            for x in xs:
                pos = k + x - p.pleat_phase
                if -1e-9 <= pos < Lp - 1e-9:
                    poss.append(max(pos, 0.0))
        poss.sort()
        ded = []
        for q in poss:
            if not ded or q - ded[-1] > 1e-9:
                ded.append(q)
        poss = ded
        if not cl:
            if not poss or poss[0] > 1e-9:
                poss.insert(0, 0.0)
            poss.append(Lp)
        cp = _resample_at(pts, [q * period for q in poss], cl)
        n, q3, wv = len(cp), [], []
        for i, c in enumerate(cp):
            prev = cp[i - 1] if (i > 0 or cl) else c
            nxt = cp[(i + 1) % n] if (i < n - 1 or cl) else c
            t = nxt - prev
            side = t.cross(d)
            if side.length < 1e-9:
                side = t.cross(Vector((1, 0, 0)))
                if side.length < 1e-9:
                    side = t.cross(Vector((0, 1, 0)))
            if side.length < 1e-9:
                side = Vector((1, 0, 0))
            side.normalize()
            w = ev(poss[i] + p.pleat_phase)
            wv.append(w)
            q3.append(c + side * (depth * w))
        # UVs in metres: unrolled length along the pleated path x height (an extruded
        # strip unrolls flat, so this is stretch-free).
        uacc = [0.0]
        for i in range(1, n):
            uacc.append(uacc[-1] + (q3[i] - q3[i - 1]).length)
        u_end = uacc[-1] + ((q3[0] - q3[-1]).length if cl else 0.0)
        if np is not None:
            _curve_strip_np(bm, q3, wv, uacc, u_end, d, H, rows, cl)
        else:
            _curve_strip_py(bm, q3, wv, uacc, u_end, d, H, rows, cl)
        if ref is not None:
            _wall_path(ref, pts, cl, d, H, p.pleat_density)


def _face_components(tmp):
    seen, comps = set(), []
    for f in tmp.faces:
        if f.index in seen:
            continue
        seen.add(f.index)
        stack, comp = [f], []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for e in cur.edges:
                for nf in e.link_faces:
                    if nf.index not in seen:
                        seen.add(nf.index)
                        stack.append(nf)
        comps.append(comp)
    return comps


def _surface_param(cos, tris, ua, ra):
    """Surface coordinates for a triangulated island.

    Returns (tri_uv, vnorm, ring) where tri_uv[t] holds the (u, v) of each corner of
    triangle t, measured ALONG the surface (u across the pleats, v along the ridges),
    vnorm are smooth area-weighted vertex normals, and ring is the loop length when the
    island wraps all the way round (else 0). A wrapping island is cut open along a seam;
    corners on the far side of the seam get the loop length added, so the strip is
    continuous in (u, v) and closes again in 3D.
    """
    nv = len(cos)
    tri_n, tri_dirs = [], []
    vnorm = [Vector((0.0, 0.0, 0.0)) for _ in range(nv)]
    for (i, j, k) in tris:
        n = (cos[j] - cos[i]).cross(cos[k] - cos[i])
        for idx in (i, j, k):
            vnorm[idx] = vnorm[idx] + n
        if n.length < 1e-15:
            tri_n.append(None)
            tri_dirs.append((ua.copy(), ra.copy()))
            continue
        n = n.normalized()
        tri_n.append(n)
        rt = ra - n * ra.dot(n)
        if rt.length < 1e-6:                        # face perpendicular to the ridge axis
            gu = ua - n * ua.dot(n)
            if gu.length < 1e-9:
                gu = ua.copy()
            gu = gu.normalized()
            rt = n.cross(gu)
        else:
            rt = rt.normalized()
            gu = rt.cross(n)
        tri_dirs.append((gu.normalized(), rt.normalized()))
    for idx in range(nv):
        vnorm[idx] = vnorm[idx].normalized() if vnorm[idx].length > 1e-15 else Vector((0.0, 0.0, 1.0))

    edge_dirs = {}
    for t, (i, j, k) in enumerate(tris):
        gu, gv = tri_dirs[t]
        for a, b in ((i, j), (j, k), (k, i)):
            key = (a, b) if a < b else (b, a)
            acc = edge_dirs.get(key)
            if acc is None:
                edge_dirs[key] = [gu.copy(), gv.copy()]
            else:
                acc[0] = acc[0] + gu
                acc[1] = acc[1] + gv
    edge_d = {}
    for (a, b), (gu, gv) in edge_dirs.items():
        gu = gu.normalized() if gu.length > 1e-9 else gu
        gv = gv.normalized() if gv.length > 1e-9 else gv
        d = cos[b] - cos[a]
        edge_d[(a, b)] = (gu.dot(d), gv.dot(d))     # change from a to b

    def delta(a, b):
        if (a, b) in edge_d:
            return edge_d[(a, b)]
        du, dv = edge_d[(b, a)]
        return -du, -dv

    nbrs = [[] for _ in range(nv)]
    for (a, b) in edge_d:
        nbrs[a].append(b)
        nbrs[b].append(a)

    best, anchor = -1.0, tris[0][0]
    for (i, j, k) in tris:
        ar = (cos[j] - cos[i]).cross(cos[k] - cos[i]).length
        if ar > best:
            best, anchor = ar, i
    u = [None] * nv
    v = [None] * nv
    for seed in [anchor] + list(range(nv)):
        if u[seed] is not None or (seed != anchor and not nbrs[seed]):
            continue
        u[seed], v[seed] = ua.dot(cos[seed]), ra.dot(cos[seed])
        queue = deque([seed])
        while queue:
            c = queue.popleft()
            for o in nbrs[c]:
                if u[o] is None:
                    du, dv = delta(c, o)
                    u[o], v[o] = u[c] + du, v[c] + dv
                    queue.append(o)
    for idx in range(nv):
        if u[idx] is None:
            u[idx], v[idx] = ua.dot(cos[idx]), ra.dot(cos[idx])

    # Seam detection: on an island that wraps round, the two integration fronts meet
    # with a mismatch of one whole loop length.
    span = max(u) - min(u) if u else 0.0
    seam = set()
    errs = []
    for (a, b) in edge_d:
        du, _dv = edge_d[(a, b)]
        err = u[b] - u[a] - du
        if span > 1e-9 and abs(err) > 0.5 * span:
            seam.add((a, b))
            errs.append(abs(err))
    ring = 0.0
    if seam:
        errs.sort()
        ring = errs[len(errs) // 2]
        # Keep the seam edges in the relaxation, but with a whole-loop offset, so the
        # two sides of the seam stay locked together (otherwise they drift and twist).
        for (a, b) in seam:
            du, dv = edge_d[(a, b)]
            err = u[b] - u[a] - du
            edge_d[(a, b)] = (du + round(err / ring) * ring, dv)

    # Gauss-Seidel relaxation (seam edges excluded) evens out curvature distortion.
    links = sum(len(nb) for nb in nbrs)
    iters = max(4, min(60, 400000 // max(1, links)))
    for _ in range(iters):
        for idx in range(nv):
            if idx == anchor or not nbrs[idx]:
                continue
            su = sv = 0.0
            for o in nbrs[idx]:
                du, dv = delta(o, idx)
                su += u[o] + du
                sv += v[o] + dv
            m = len(nbrs[idx])
            u[idx], v[idx] = su / m, sv / m

    def is_seam(a, b):
        return (a, b) in seam or (b, a) in seam

    tri_uv = []
    for t, tri in enumerate(tris):
        corners = [(u[x], v[x]) for x in tri]
        if ring > 0.0 and any(is_seam(tri[a], tri[(a + 1) % 3]) for a in range(3)):
            # Keep the vertices reachable from corner 0 without crossing the seam;
            # shift the rest by a whole loop so the triangle is continuous in (u, v).
            same = {0}
            for _ in range(2):
                for a in range(3):
                    for b in range(3):
                        if a in same and b not in same and not is_seam(tri[a], tri[b]):
                            same.add(b)
            r = tri[0]
            gu, gv = tri_dirs[t]
            for c in range(3):
                if c in same:
                    continue
                w = tri[c]
                est = u[r] + gu.dot(cos[w] - cos[r])
                shift = round((est - u[w]) / ring) * ring
                corners[c] = (u[w] + shift, v[w])
        tri_uv.append(corners)
    return tri_uv, vnorm, ring


def _clip_poly_halfplane(poly, a, b, eps):
    """Sutherland-Hodgman step: keep the left side of the directed line a->b.

    poly items are (u, v, sides) where sides is a frozenset of grid-line ids the
    point lies on. An intersection point lies on every line both endpoints share.
    """
    out = []
    n = len(poly)
    if n == 0:
        return out
    ex, ey = b[0] - a[0], b[1] - a[1]

    def side(pt):
        return ex * (pt[1] - a[1]) - ey * (pt[0] - a[0])

    for idx in range(n):
        P = poly[idx]
        Q = poly[(idx + 1) % n]
        sp, sq = side(P), side(Q)
        pin, qin = sp >= -eps, sq >= -eps
        if pin:
            out.append(P)
        if pin != qin:
            t = sp / (sp - sq) if abs(sp - sq) > 1e-300 else 0.0
            t = min(1.0, max(0.0, t))
            out.append((P[0] + (Q[0] - P[0]) * t, P[1] + (Q[1] - P[1]) * t, P[2] & Q[2]))
    return out


def _bary(px, py, A, B, C):
    d = (B[1] - C[1]) * (A[0] - C[0]) + (C[0] - B[0]) * (A[1] - C[1])
    if abs(d) < 1e-300:
        return None
    w0 = ((B[1] - C[1]) * (px - C[0]) + (C[0] - B[0]) * (py - C[1])) / d
    w1 = ((C[1] - A[1]) * (px - C[0]) + (A[0] - C[0]) * (py - C[1])) / d
    return w0, w1, 1.0 - w0 - w1


def _seg_hits_rect(p, q, u0, u1, v0, v1, tol):
    """Segment p-q touches the closed rectangle (Liang-Barsky with tolerance)."""
    u0, u1, v0, v1 = u0 - tol, u1 + tol, v0 - tol, v1 + tol
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for pp, qq in ((-dx, p[0] - u0), (dx, u1 - p[0]), (-dy, p[1] - v0), (dy, v1 - p[1])):
        if abs(pp) < 1e-300:
            if qq < 0:
                return False
        else:
            r = qq / pp
            if pp < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                return False
    return True


def _grid_mesh(cos, vnorm, tris, tri_uv, UL, VL, qstep):
    """Quad-grid remesh of an island in (u, v) space.

    UL / VL: sorted grid lines covering the island. Cells fully inside become one
    quad made of the four grid corners; cells crossed by the island outline are
    clipped against the triangles, merged back into one polygon and stripped of
    any vertex that only existed because a source edge crossed the cell, so the
    whole result is free of T-junctions.

    Returns (verts, faces): verts maps key -> (position, normal, u);
    faces is a list of key lists (counter-clockwise in u, v).
    """
    nU, nV = len(UL) - 1, len(VL) - 1
    if nU < 1 or nV < 1:
        return {}, []
    tol = qstep * 10.0

    def cells_of(lo_u, hi_u, lo_v, hi_v):
        a0 = max(0, bisect.bisect_right(UL, lo_u - tol) - 1)
        a1 = min(nU - 1, bisect.bisect_left(UL, hi_u + tol))
        b0 = max(0, bisect.bisect_right(VL, lo_v - tol) - 1)
        b1 = min(nV - 1, bisect.bisect_left(VL, hi_v + tol))
        return a0, a1, b0, b1

    # Orient every triangle counter-clockwise in (u, v).
    tuv = []
    for t in range(len(tris)):
        A, B, C = tri_uv[t]
        area = (B[0] - A[0]) * (C[1] - A[1]) - (B[1] - A[1]) * (C[0] - A[0])
        tuv.append((A, B, C, area > 0.0, abs(area) > 1e-24))

    cell_tris = {}
    for t, (A, B, C, _ccw, ok) in enumerate(tuv):
        if not ok:
            continue
        us, vs = (A[0], B[0], C[0]), (A[1], B[1], C[1])
        a0, a1, b0, b1 = cells_of(min(us), max(us), min(vs), max(vs))
        for a in range(a0, a1 + 1):
            for b in range(b0, b1 + 1):
                cell_tris.setdefault((a, b), []).append(t)

    # Outline of the island in (u, v): triangle edges without a matching twin.
    edge_uv = {}
    for t, tri in enumerate(tris):
        if not tuv[t][4]:
            continue
        for c in range(3):
            a, b = tri[c], tri[(c + 1) % 3]
            key = (a, b) if a < b else (b, a)
            pa, pb = tri_uv[t][c], tri_uv[t][(c + 1) % 3]
            if a > b:
                pa, pb = pb, pa
            edge_uv.setdefault(key, []).append((pa, pb))
    complex_cells = set()
    cell_outline = {}

    def same(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    def on_grid_line(pa, pb):
        """Outline segment running exactly along a grid line: the cells beside it need no clipping."""
        for lines, ax in ((UL, 0), (VL, 1)):
            i = bisect.bisect_left(lines, pa[ax])
            for j in (i - 1, i):
                if 0 <= j < len(lines) and abs(lines[j] - pa[ax]) <= tol and abs(lines[j] - pb[ax]) <= tol:
                    return True
        return False

    for key, uses in edge_uv.items():
        if len(uses) == 2 and same(uses[0][0], uses[1][0]) and same(uses[0][1], uses[1][1]):
            continue
        for (pa, pb) in uses:
            if on_grid_line(pa, pb):
                continue
            a0, a1, b0, b1 = cells_of(min(pa[0], pb[0]), max(pa[0], pb[0]),
                                      min(pa[1], pb[1]), max(pa[1], pb[1]))
            for a in range(a0, a1 + 1):
                for b in range(b0, b1 + 1):
                    if _seg_hits_rect(pa, pb, UL[a], UL[a + 1], VL[b], VL[b + 1], tol):
                        complex_cells.add((a, b))
                        cell_outline.setdefault((a, b), []).append((pa, pb))

    verts = {}

    def put(key, t, pu, pv):
        if key in verts:
            return
        w = _bary(pu, pv, *tri_uv[t])
        if w is None:
            return
        i, j, k = tris[t]
        pos = cos[i] * w[0] + cos[j] * w[1] + cos[k] * w[2]
        nrm = vnorm[i] * w[0] + vnorm[j] * w[1] + vnorm[k] * w[2]
        nrm = nrm.normalized() if nrm.length > 1e-12 else vnorm[i].copy()
        verts[key] = (pos, nrm, pu, pv)

    # Grid corners: which triangle contains each one.
    corner_ok = {}

    def locate(a, b):
        pu, pv = UL[a], VL[b]
        seen = set()
        for ca in (a - 1, a):
            for cb in (b - 1, b):
                for t in cell_tris.get((ca, cb), ()):
                    if t in seen:
                        continue
                    seen.add(t)
                    w = _bary(pu, pv, *tri_uv[t])
                    if w is not None and min(w) >= -1e-9:
                        return t
        return None

    faces = []
    for (a, b), tlist in cell_tris.items():
        if (a, b) in complex_cells:
            continue
        ok = True
        for (ca, cb) in ((a, b), (a + 1, b), (a + 1, b + 1), (a, b + 1)):
            if (ca, cb) not in corner_ok:
                corner_ok[(ca, cb)] = locate(ca, cb)
            if corner_ok[(ca, cb)] is None:
                ok = False
        if not ok:
            complex_cells.add((a, b))
            continue
        quad = []
        for (ca, cb) in ((a, b), (a + 1, b), (a + 1, b + 1), (a, b + 1)):
            key = ('c', ca, cb)
            put(key, corner_ok[(ca, cb)], UL[ca], VL[cb])
            quad.append(key)
        faces.append(quad)

    def near_line(lines, x):
        i = bisect.bisect_left(lines, x)
        for j in (i - 1, i):
            if 0 <= j < len(lines) and abs(lines[j] - x) <= tol:
                return j
        return None

    def key_of(pt):
        """Canonical vertex name: snapped to grid corners / grid lines when on them."""
        pu, pv = pt[0], pt[1]
        a = near_line(UL, pu)
        b = near_line(VL, pv)
        if a is not None and b is not None:
            return ('c', a, b), UL[a], VL[b]
        if a is not None:
            return ('U', a, int(round(pv / qstep))), UL[a], pv
        if b is not None:
            return ('V', b, int(round(pu / qstep))), pu, VL[b]
        return ('P', int(round(pu / qstep)), int(round(pv / qstep))), pu, pv

    def key_sides(key):
        if key[0] == 'c':
            return {('U', key[1]), ('V', key[2])}
        if key[0] == 'U':
            return {('U', key[1])}
        if key[0] == 'V':
            return {('V', key[1])}
        return set()

    protected = set()

    def on_outline(key, pu, pv, segs):
        """Point lies on an outline segment that is not simply running along its grid line."""
        axis = 0 if key[0] == 'U' else 1
        line = UL[key[1]] if key[0] == 'U' else VL[key[1]]
        for (pa, pb) in segs:
            if abs(pa[axis] - line) <= tol and abs(pb[axis] - line) <= tol:
                continue
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            ll = dx * dx + dy * dy
            tt = 0.0 if ll < 1e-300 else max(0.0, min(1.0, ((pu - pa[0]) * dx + (pv - pa[1]) * dy) / ll))
            ex, ey = pa[0] + dx * tt - pu, pa[1] + dy * tt - pv
            if ex * ex + ey * ey <= (tol * 10.0) ** 2:
                return True
        return False

    for (a, b) in sorted(complex_cells):
        tlist = cell_tris.get((a, b))
        if not tlist:
            continue
        segs = cell_outline.get((a, b), ())
        u0, u1, v0, v1 = UL[a], UL[a + 1], VL[b], VL[b + 1]
        rect = [(u0, v0, frozenset({('U', a), ('V', b)})),
                (u1, v0, frozenset({('U', a + 1), ('V', b)})),
                (u1, v1, frozenset({('U', a + 1), ('V', b + 1)})),
                (u0, v1, frozenset({('U', a), ('V', b + 1)}))]
        pieces = []
        for t in tlist:
            A, B, C, ccw, ok = tuv[t]
            if not ok:
                continue
            tri = (A, B, C) if ccw else (A, C, B)
            poly = rect
            for e in range(3):
                poly = _clip_poly_halfplane(poly, tri[e], tri[(e + 1) % 3], qstep)
                if len(poly) < 3:
                    break
            if len(poly) < 3:
                continue
            keys = []
            for pt in poly:
                key, pu, pv = key_of(pt)
                put(key, t, pu, pv)
                if key[0] in ('U', 'V') and key not in protected and segs and on_outline(key, pu, pv, segs):
                    protected.add(key)
                if key in verts and (not keys or keys[-1] != key):
                    keys.append(key)
            if len(keys) > 2 and keys[0] == keys[-1]:
                keys.pop()
            if len(keys) >= 3:
                pieces.append(keys)
        if not pieces:
            continue
        # Merge the pieces: interior edges appear twice in opposite directions.
        count = {}
        for keys in pieces:
            for x, y in zip(keys, keys[1:] + keys[:1]):
                count[(x, y)] = count.get((x, y), 0) + 1
        for (x, y) in list(count):
            if count.get((x, y), 0) > 0 and count.get((y, x), 0) > 0:
                m = min(count[(x, y)], count[(y, x)])
                count[(x, y)] -= m
                count[(y, x)] -= m
        nxt = {}
        for (x, y), c in count.items():
            for _ in range(c):
                nxt.setdefault(x, []).append(y)
        while nxt:
            start = next(iter(nxt))
            loop, cur = [start], start
            for _ in range(10000):
                outs = nxt.get(cur)
                if not outs:
                    break
                nx = outs.pop()
                if not outs:
                    del nxt[cur]
                if nx == start:
                    break
                loop.append(nx)
                cur = nx
            # Drop points that only sit on a cell side because a source edge crossed it.
            changed = True
            while changed and len(loop) > 3:
                changed = False
                for idx in range(len(loop)):
                    k = loop[idx]
                    if k[0] == 'c' or k[0] == 'P' or k in protected:
                        continue
                    pk, nk = loop[idx - 1], loop[(idx + 1) % len(loop)]
                    s = key_sides(k)
                    if s & key_sides(pk) and s & key_sides(nk):
                        del loop[idx]
                        changed = True
                        break
            if len(loop) >= 3:
                faces.append(loop)
    return verts, faces


def _grid_mesh_np(cos, vnorm, tris, tri_uv, UL, VL, qstep):
    """numpy version of _grid_mesh (same topology, same outline handling).

    Interior cells are found and built with array maths; only the cells crossed by
    the outline go through the clipping / merging path. Returns
    (positions (N,3), normals (N,3), u (N,), v (N,), faces as vertex-index lists).
    """
    nU, nV = len(UL) - 1, len(VL) - 1
    if nU < 1 or nV < 1 or not tris:
        return None
    tol = qstep * 10.0
    ULa = np.asarray(UL, dtype=np.float64)
    VLa = np.asarray(VL, dtype=np.float64)
    T = len(tris)
    TUV = np.asarray(tri_uv, dtype=np.float64)                  # (T, 3, 2)
    TI = np.asarray(tris, dtype=np.int64)                       # (T, 3)
    P = np.asarray([tuple(c) for c in cos], dtype=np.float64)   # (V, 3)
    NRM = np.asarray([tuple(n) for n in vnorm], dtype=np.float64)
    A, B, C = TUV[:, 0], TUV[:, 1], TUV[:, 2]
    area2 = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])
    valid = np.abs(area2) > 1e-24
    tmin = TUV.min(axis=1)
    tmax = TUV.max(axis=1)

    def cell_range(lo, hi, lines, n):
        a0 = max(0, bisect.bisect_right(lines, lo - tol) - 1)
        a1 = min(n - 1, bisect.bisect_left(lines, hi + tol))
        return a0, a1

    # ---- grid corners: which triangle holds each (vectorised per triangle) ----
    ctri = np.full((nU + 1, nV + 1), -1, dtype=np.int64)
    cw = np.zeros((nU + 1, nV + 1, 3), dtype=np.float64)
    for t in range(T):
        if not valid[t]:
            continue
        a0 = bisect.bisect_left(UL, tmin[t, 0] - tol)
        a1 = bisect.bisect_right(UL, tmax[t, 0] + tol)
        b0 = bisect.bisect_left(VL, tmin[t, 1] - tol)
        b1 = bisect.bisect_right(VL, tmax[t, 1] + tol)
        if a1 <= a0 or b1 <= b0:
            continue
        uu = ULa[a0:a1][:, None]
        vv = VLa[b0:b1][None, :]
        (ax, ay), (bx, by), (cx, cy) = TUV[t]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        w0 = ((by - cy) * (uu - cx) + (cx - bx) * (vv - cy)) / d
        w1 = ((cy - ay) * (uu - cx) + (ax - cx) * (vv - cy)) / d
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        sub = ctri[a0:a1, b0:b1]
        take = inside & (sub < 0)
        if take.any():
            sub[take] = t
            cw[a0:a1, b0:b1][take] = np.stack([w0, w1, w2], axis=-1)[take]

    # ---- cells crossed by the island outline ----
    edge_uv = {}
    for t, tri in enumerate(tris):
        if not valid[t]:
            continue
        for c in range(3):
            a, b = tri[c], tri[(c + 1) % 3]
            key = (a, b) if a < b else (b, a)
            pa, pb = tri_uv[t][c], tri_uv[t][(c + 1) % 3]
            if a > b:
                pa, pb = pb, pa
            edge_uv.setdefault(key, []).append((pa, pb))
    complex_cells = set()
    cell_outline = {}

    def same(p_, q_):
        return abs(p_[0] - q_[0]) <= tol and abs(p_[1] - q_[1]) <= tol

    def on_grid_line(pa, pb):
        """Outline segment running exactly along a grid line: the cells beside it need no clipping."""
        for lines, ax in ((UL, 0), (VL, 1)):
            i = bisect.bisect_left(lines, pa[ax])
            for j in (i - 1, i):
                if 0 <= j < len(lines) and abs(lines[j] - pa[ax]) <= tol and abs(lines[j] - pb[ax]) <= tol:
                    return True
        return False

    for key, uses in edge_uv.items():
        if len(uses) == 2 and same(uses[0][0], uses[1][0]) and same(uses[0][1], uses[1][1]):
            continue
        for (pa, pb) in uses:
            if on_grid_line(pa, pb):
                continue
            a0, a1 = cell_range(min(pa[0], pb[0]), max(pa[0], pb[0]), UL, nU)
            b0, b1 = cell_range(min(pa[1], pb[1]), max(pa[1], pb[1]), VL, nV)
            for a in range(a0, a1 + 1):
                for b in range(b0, b1 + 1):
                    if _seg_hits_rect(pa, pb, UL[a], UL[a + 1], VL[b], VL[b + 1], tol):
                        complex_cells.add((a, b))
                        cell_outline.setdefault((a, b), []).append((pa, pb))

    located = ctri >= 0
    full = located[:-1, :-1] & located[1:, :-1] & located[1:, 1:] & located[:-1, 1:]
    cmask = np.zeros((nU, nV), dtype=bool)
    if complex_cells:
        ca = np.fromiter((c[0] for c in complex_cells), dtype=np.int64, count=len(complex_cells))
        cb = np.fromiter((c[1] for c in complex_cells), dtype=np.int64, count=len(complex_cells))
        cmask[ca, cb] = True
    simple = full & ~cmask
    # partly located cells that the outline does not cross still need clipping
    anyc = located[:-1, :-1] | located[1:, :-1] | located[1:, 1:] | located[:-1, 1:]
    for a, b in zip(*np.nonzero(anyc & ~full & ~cmask)):
        complex_cells.add((int(a), int(b)))

    # ---- corner vertices (only the ones a face will use are kept later) ----
    pos_list, nrm_list, u_list, v_list = [], [], [], []
    corner_vid = np.full((nU + 1, nV + 1), -1, dtype=np.int64)
    li, lj = np.nonzero(located)
    if len(li):
        tt = ctri[li, lj]
        w = cw[li, lj]
        tv = TI[tt]
        cpos = (P[tv] * w[..., None]).sum(axis=1)
        cn = (NRM[tv] * w[..., None]).sum(axis=1)
        ln = np.linalg.norm(cn, axis=1)
        bad = ln < 1e-12
        cn[~bad] /= ln[~bad, None]
        cn[bad] = NRM[tv[bad, 0]]
        corner_vid[li, lj] = np.arange(len(li))
        pos_list.append(cpos)
        nrm_list.append(cn)
        u_list.append(ULa[li])
        v_list.append(VLa[lj])
    n_corner = len(li)

    # interior quads, counter-clockwise in (u, v)
    si, sj = np.nonzero(simple)
    quads = np.stack([corner_vid[si, sj], corner_vid[si + 1, sj],
                      corner_vid[si + 1, sj + 1], corner_vid[si, sj + 1]], axis=1) if len(si) else \
        np.zeros((0, 4), dtype=np.int64)
    faces = quads.tolist()

    # ---- outline cells: clip against the triangles, merge, strip extra points ----
    extra_pos, extra_nrm, extra_u, extra_v = [], [], [], []
    extra_id = {}

    def near_line(lines, x):
        i = bisect.bisect_left(lines, x)
        for j in (i - 1, i):
            if 0 <= j < len(lines) and abs(lines[j] - x) <= tol:
                return j
        return None

    def key_of(pu, pv):
        a = near_line(UL, pu)
        b = near_line(VL, pv)
        if a is not None and b is not None:
            return ('c', a, b), UL[a], VL[b]
        if a is not None:
            return ('U', a, int(round(pv / qstep))), UL[a], pv
        if b is not None:
            return ('V', b, int(round(pu / qstep))), pu, VL[b]
        return ('P', int(round(pu / qstep)), int(round(pv / qstep))), pu, pv

    def vid_of(key, t, pu, pv):
        if key[0] == 'c':
            v = int(corner_vid[key[1], key[2]])
            if v >= 0:
                return v
        v = extra_id.get(key)
        if v is not None:
            return v
        w_ = _bary(pu, pv, *tri_uv[t])
        if w_ is None:
            return None
        i, j, k = tris[t]
        pos = P[i] * w_[0] + P[j] * w_[1] + P[k] * w_[2]
        nrm = NRM[i] * w_[0] + NRM[j] * w_[1] + NRM[k] * w_[2]
        ln_ = float(np.sqrt((nrm * nrm).sum()))
        nrm = nrm / ln_ if ln_ > 1e-12 else NRM[i]
        v = n_corner + len(extra_pos)
        extra_pos.append(pos)
        extra_nrm.append(nrm)
        extra_u.append(pu)
        extra_v.append(pv)
        extra_id[key] = v
        return v

    def key_sides(key):
        if key[0] == 'c':
            return {('U', key[1]), ('V', key[2])}
        if key[0] == 'U':
            return {('U', key[1])}
        if key[0] == 'V':
            return {('V', key[1])}
        return set()

    def on_outline(key, pu, pv, segs):
        axis = 0 if key[0] == 'U' else 1
        line = UL[key[1]] if key[0] == 'U' else VL[key[1]]
        for (pa, pb) in segs:
            if abs(pa[axis] - line) <= tol and abs(pb[axis] - line) <= tol:
                continue
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            ll = dx * dx + dy * dy
            tt_ = 0.0 if ll < 1e-300 else max(0.0, min(1.0, ((pu - pa[0]) * dx + (pv - pa[1]) * dy) / ll))
            ex, ey = pa[0] + dx * tt_ - pu, pa[1] + dy * tt_ - pv
            if ex * ex + ey * ey <= (tol * 10.0) ** 2:
                return True
        return False

    protected = set()
    for (a, b) in sorted(complex_cells):
        u0, u1, v0, v1 = UL[a], UL[a + 1], VL[b], VL[b + 1]
        cand = np.nonzero(valid & (tmin[:, 0] <= u1 + tol) & (tmax[:, 0] >= u0 - tol)
                          & (tmin[:, 1] <= v1 + tol) & (tmax[:, 1] >= v0 - tol))[0]
        if not len(cand):
            continue
        segs = cell_outline.get((a, b), ())
        rect = [(u0, v0, frozenset()), (u1, v0, frozenset()), (u1, v1, frozenset()), (u0, v1, frozenset())]
        pieces = []
        for t in cand.tolist():
            tA, tB, tC = tri_uv[t]
            ccw = area2[t] > 0.0
            tri = (tA, tB, tC) if ccw else (tA, tC, tB)
            poly = rect
            for e in range(3):
                poly = _clip_poly_halfplane(poly, tri[e], tri[(e + 1) % 3], qstep)
                if len(poly) < 3:
                    break
            if len(poly) < 3:
                continue
            keys = []
            for pt in poly:
                key, pu, pv = key_of(pt[0], pt[1])
                if vid_of(key, t, pu, pv) is None:
                    continue
                if key[0] in ('U', 'V') and key not in protected and segs and on_outline(key, pu, pv, segs):
                    protected.add(key)
                if not keys or keys[-1] != key:
                    keys.append(key)
            if len(keys) > 2 and keys[0] == keys[-1]:
                keys.pop()
            if len(keys) >= 3:
                pieces.append(keys)
        if not pieces:
            continue
        count = {}
        for keys in pieces:
            for x, y in zip(keys, keys[1:] + keys[:1]):
                count[(x, y)] = count.get((x, y), 0) + 1
        for (x, y) in list(count):
            if count.get((x, y), 0) > 0 and count.get((y, x), 0) > 0:
                m = min(count[(x, y)], count[(y, x)])
                count[(x, y)] -= m
                count[(y, x)] -= m
        nxt = {}
        for (x, y), c in count.items():
            for _ in range(c):
                nxt.setdefault(x, []).append(y)
        while nxt:
            start = next(iter(nxt))
            loop, cur = [start], start
            for _ in range(10000):
                outs = nxt.get(cur)
                if not outs:
                    break
                nx = outs.pop()
                if not outs:
                    del nxt[cur]
                if nx == start:
                    break
                loop.append(nx)
                cur = nx
            changed = True
            while changed and len(loop) > 3:
                changed = False
                for idx in range(len(loop)):
                    k = loop[idx]
                    if k[0] == 'c' or k[0] == 'P' or k in protected:
                        continue
                    pk, nk = loop[idx - 1], loop[(idx + 1) % len(loop)]
                    s = key_sides(k)
                    if s & key_sides(pk) and s & key_sides(nk):
                        del loop[idx]
                        changed = True
                        break
            if len(loop) >= 3:
                ids = []
                for k in loop:
                    if k[0] == 'c' and corner_vid[k[1], k[2]] >= 0:
                        v = int(corner_vid[k[1], k[2]])
                    else:
                        v = extra_id.get(k)
                    if v is not None and (not ids or ids[-1] != v):
                        ids.append(v)
                if len(ids) > 2 and ids[0] == ids[-1]:
                    ids.pop()
                if len(ids) >= 3:
                    faces.append(ids)

    if extra_pos:
        pos_list.append(np.asarray(extra_pos))
        nrm_list.append(np.asarray(extra_nrm))
        u_list.append(np.asarray(extra_u))
        v_list.append(np.asarray(extra_v))
    if not pos_list:
        return None
    pos = np.concatenate(pos_list) if len(pos_list) > 1 else pos_list[0]
    nrm = np.concatenate(nrm_list) if len(nrm_list) > 1 else nrm_list[0]
    us = np.concatenate(u_list) if len(u_list) > 1 else u_list[0]
    vs = np.concatenate(v_list) if len(v_list) > 1 else v_list[0]
    return pos, nrm, us, vs, faces


UV_LAYER = "UVMap"
PLEAT_ATTR = "fm_pleat"          # pleat profile value per vertex: +1 ridge, -1 valley


def _arc_table(table, period, depth):
    """Cumulative surface length over one pleat period (the profile unrolled flat).
    Returns (xs, cumulative lengths, length of one period)."""
    pts = list(table) + [(table[0][0] + 1.0, table[0][1])]
    xs, cum, acc = [pts[0][0]], [0.0], 0.0
    for (xa, wa), (xb, wb) in zip(pts, pts[1:]):
        acc += math.hypot((xb - xa) * period, (wb - wa) * depth)
        xs.append(xb)
        cum.append(acc)
    return xs, cum, acc


def _arc_np(arc, x):
    """Unrolled length at pleat coordinate x (in periods), vectorised."""
    xs, cum, per = arc
    x0 = xs[0]
    k = np.floor(x - x0)
    return k * per + np.interp(x - x0 - k + x0, xs, cum)


def _arc_py(arc, x):
    xs, cum, per = arc
    x0 = xs[0]
    k = math.floor(x - x0)
    xm = x - x0 - k + x0
    i = max(0, min(len(xs) - 2, bisect.bisect_right(xs, xm) - 1))
    span = xs[i + 1] - xs[i]
    t = 0.0 if span < 1e-12 else (xm - xs[i]) / span
    return k * per + cum[i] + (cum[i + 1] - cum[i]) * t


def _profile_np(table, x):
    """Vectorised _make_eval: periodic piecewise-linear pleat profile."""
    xs = np.asarray([q[0] for q in table] + [table[0][0] + 1.0])
    ws = np.asarray([q[1] for q in table] + [table[0][1]])
    x0 = xs[0]
    return np.interp((x - x0) % 1.0 + x0, xs, ws)


def _grid_lines(lo, hi, positions, tol, ends=True, min_gap=0.0):
    """Sorted, de-duplicated grid lines.

    ends=True adds the exact extent lo / hi (clean outer cells) and drops lines that
    would leave a sliver narrower than min_gap next to them. ends=False keeps the
    pattern strictly periodic (needed to close a wrap-around seam exactly) and just
    makes sure the lines cover [lo, hi].
    """
    if ends:
        vals = [lo, hi] + [x for x in positions if lo + max(tol, min_gap) < x < hi - max(tol, min_gap)]
    else:
        vals = sorted(positions)
        inside = [x for x in vals if lo - tol <= x <= hi + tol]
        below = [x for x in vals if x < lo - tol]
        above = [x for x in vals if x > hi + tol]
        vals = ([below[-1]] if below else []) + inside + ([above[0]] if above else [])
    vals.sort()
    out = []
    for x in vals:
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def _pleat_grid_lines(umin, umax, vmin, vmax, base, period, xs, vstep, ring, qstep):
    """u lines at every pleat-profile cut, v lines every vstep."""
    upos = []
    k0 = int(math.floor((umin - base) / period)) - 1
    k1 = int(math.ceil((umax - base) / period)) + 2
    for kk in range(k0, k1):
        for x in xs:
            upos.append(base + (kk + x) * period)
    step = period / max(1, len(xs))
    vpos = [m * vstep for m in range(int(math.floor(vmin / vstep)) - 1, int(math.ceil(vmax / vstep)) + 2)]
    tol = qstep * 100
    if ring > 0.0:
        UL = _grid_lines(umin, umax, upos, tol, ends=False)
    else:
        UL = _grid_lines(umin, umax, upos, tol, ends=True, min_gap=0.3 * step)
    VL = _grid_lines(vmin, vmax, vpos, tol, ends=True, min_gap=0.3 * vstep)
    return UL, VL


def _face_component(bm, p, comp):
    """Pleat a connected face island as one continuous, cleanly quadded surface.

    - pleat spacing is measured along the surface (no stretching on angled faces)
    - topology is a structured grid: every pleat / bevel cut is an edge loop, cells
      inside the selection are perfect quads, only the outline gets clipped faces
    - pleats are pushed out along smooth (interpolated) normals, so there are no
      shading breaks where the selected faces meet
    - a selection that wraps all the way round is closed with a seamless pleat
    """
    if not comp:
        return
    f0 = max(comp, key=lambda f: f.calc_area())
    n0 = f0.normal.normalized()
    ref = Vector((1.0, 0.0, 0.0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0.0, 1.0, 0.0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0).normalized()
    angle = p.pleat_angle
    ua = (u0 * math.cos(angle) + v0 * math.sin(angle)).normalized()
    ra = n0.cross(ua).normalized()

    src = bmesh.new()
    try:
        vm = {}
        for f in comp:
            vl = []
            for vert in f.verts:
                if vert not in vm:
                    vm[vert] = src.verts.new(vert.co)
                vl.append(vm[vert])
            try:
                src.faces.new(vl)
            except ValueError:
                pass
        bmesh.ops.triangulate(src, faces=src.faces[:])
        src.verts.index_update()
        cos = [vert.co.copy() for vert in src.verts]
        tris = [tuple(vert.index for vert in f.verts) for f in src.faces if len(f.verts) == 3]
    finally:
        src.free()
    if not tris:
        return

    _t0 = time.perf_counter()
    tri_uv, vnorm, ring = _surface_param(cos, tris, ua, ra)
    _FACE_TIMINGS["param_ms"] = _FACE_TIMINGS.get("param_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    all_u = [c[0] for tri in tri_uv for c in tri]
    all_v = [c[1] for tri in tri_uv for c in tri]
    umin, umax, vmin, vmax = min(all_u), max(all_u), min(all_v), max(all_v)
    size = max(umax - umin, vmax - vmin, 1e-6)
    qstep = size * 1e-9

    period = 1.0 / max(p.pleat_density, 1e-3)
    if ring > 0.0:                                 # close a wrap-around with a whole number of pleats
        period = ring / max(1, int(round(ring / period)))
    depth = p.pleat_depth
    rp, rv = _bevel_radii(p)
    table = _profile_table(rp, rv, p.bevel_cuts)
    ev = _make_eval(table)
    base = umin - period * p.pleat_phase

    maxlen = 1.0 / max(p.detail, 1e-3)
    xs = _cycle_xs(table, period, depth, maxlen)
    for _ in range(12):
        if (umax - umin) / period * len(xs) <= MAX_CUTS:
            break
        maxlen *= 1.5
        xs = _cycle_xs(table, period, depth, maxlen)
    vstep = 1.0 / max(p.detail, 1e-3)
    while (vmax - vmin) / vstep > MAX_CUTS:
        vstep *= 1.5
    UL, VL = _pleat_grid_lines(umin, umax, vmin, vmax, base, period, xs, vstep, ring, qstep)

    if np is not None:
        try:
            if _face_component_np(bm, p, cos, vnorm, tris, tri_uv, UL, VL, qstep, table, base,
                                  period, depth, size):
                return
        except Exception:
            traceback.print_exc()           # fall back to the pure-Python path below
    _t0 = time.perf_counter()
    verts, faces = _grid_mesh(cos, vnorm, tris, tri_uv, UL, VL, qstep)
    _FACE_TIMINGS["grid_ms"] = _FACE_TIMINGS.get("grid_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    if not faces:
        return
    _t0 = time.perf_counter()

    used = {key for keys in faces for key in keys}
    new_verts, vuv = {}, {}
    arc = _arc_table(table, period, depth)
    uvl = bm.loops.layers.uv.get(UV_LAYER) or bm.loops.layers.uv.new(UV_LAYER)
    pl = bm.verts.layers.float.get(PLEAT_ATTR) or bm.verts.layers.float.new(PLEAT_ATTR)
    for key, (pos, nrm, pu, pv) in verts.items():
        if key in used:
            x = (pu - base) / period
            w = ev(x)
            vert = bm.verts.new(pos + nrm * (depth * w))
            vert[pl] = w
            new_verts[key] = vert
            vuv[vert] = (_arc_py(arc, x), pv)
    created = []
    for keys in faces:
        vl = []
        for key in keys:
            vert = new_verts.get(key)
            if vert is not None and (not vl or vl[-1] is not vert):
                vl.append(vert)
        if len(vl) > 2 and vl[0] is vl[-1]:
            vl.pop()
        if len(vl) < 3:
            continue
        try:
            f = bm.faces.new(vl)
        except ValueError:
            continue
        for lp in f.loops:
            lp[uvl].uv = vuv[lp.vert]
        created.append(f)
    _FACE_TIMINGS["assemble_ms"] = _FACE_TIMINGS.get("assemble_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    _t0 = time.perf_counter()
    vlist = list(new_verts.values())
    # Close wrap-around seams and swallow slivers along the outline.
    outline = list({vert for f in created if f.is_valid for e in f.edges
                    if len(e.link_faces) == 1 for vert in e.verts})
    if outline:
        bmesh.ops.remove_doubles(bm, verts=outline, dist=size * 1e-4)
    vlist = [vert for vert in vlist if vert.is_valid]
    bmesh.ops.remove_doubles(bm, verts=vlist, dist=size * 1e-7)
    created = [f for f in created if f.is_valid]
    if p.mesh_type == 'TRIS':
        bmesh.ops.triangulate(bm, faces=created, quad_method='BEAUTY', ngon_method='BEAUTY')
    else:
        ngons = [f for f in created if len(f.verts) > 4]
        if ngons:
            bmesh.ops.triangulate(bm, faces=ngons, quad_method='BEAUTY', ngon_method='BEAUTY')
    _FACE_TIMINGS["cleanup_ms"] = _FACE_TIMINGS.get("cleanup_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0


def _mesh_from_arrays(me, coords, faces):
    """Fill an empty mesh from vertex coordinates and polygon index lists with
    foreach_set (several times faster than from_pydata on big grids)."""
    nv = len(coords)
    sizes = np.fromiter((len(f) for f in faces), dtype=np.int32, count=len(faces))
    loops = np.fromiter((i for f in faces for i in f), dtype=np.int32, count=int(sizes.sum()))
    starts = np.zeros(len(faces), dtype=np.int32)
    if len(faces) > 1:
        starts[1:] = np.cumsum(sizes)[:-1]
    me.vertices.add(nv)
    me.vertices.foreach_set("co", np.asarray(coords, dtype=np.float32).ravel())
    me.loops.add(len(loops))
    me.loops.foreach_set("vertex_index", loops)
    me.polygons.add(len(faces))
    me.polygons.foreach_set("loop_start", starts)
    try:
        me.polygons.foreach_set("loop_total", sizes)
    except Exception:
        pass                                  # read-only in newer Blender (derived from loop_start)
    me.update(calc_edges=True)
    me.validate(clean_customdata=False)


def _face_component_np(bm, p, cos, vnorm, tris, tri_uv, UL, VL, qstep, table, base, period, depth, size):
    """Fast path: numpy grid, vectorised pleat displacement, C-speed mesh assembly."""
    _t0 = time.perf_counter()
    res = _grid_mesh_np(cos, vnorm, tris, tri_uv, UL, VL, qstep)
    _FACE_TIMINGS["grid_ms"] = _FACE_TIMINGS.get("grid_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    if res is None:
        return True
    pos, nrm, us, vs, faces = res
    if not faces:
        return True
    _t0 = time.perf_counter()
    # keep only vertices that a face uses, and renumber
    used = np.zeros(len(pos), dtype=bool)
    for f in faces:
        used[f] = True
    remap = np.full(len(pos), -1, dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    xq = (us[used] - base) / period
    prof = _profile_np(table, xq)
    coords = pos[used] + nrm[used] * (depth * prof)[:, None]
    # UVs in metres: unrolled length across the pleats x length along them, so the
    # texture keeps its proportions on every fold (no stretching on flanks or bevels).
    uv_u = _arc_np(_arc_table(table, period, depth), xq)
    uv_v = vs[used]
    faces = [remap[f].tolist() for f in faces]
    n_quads = 0
    for f in faces:
        if len(f) != 4:
            break
        n_quads += 1
    tmp_me = bpy.data.meshes.new(".FM grid tmp")
    try:
        _mesh_from_arrays(tmp_me, coords, faces)
        loop_v = np.empty(len(tmp_me.loops), dtype=np.int32)
        tmp_me.loops.foreach_get("vertex_index", loop_v)
        uvl = tmp_me.uv_layers.new(name=UV_LAYER)
        uvl.data.foreach_set("uv", np.stack([uv_u[loop_v], uv_v[loop_v]], axis=1).astype(np.float32).ravel())
        attr = tmp_me.attributes.new(PLEAT_ATTR, 'FLOAT', 'POINT')
        attr.data.foreach_set("value", prof.astype(np.float32))
        tmp_me.update()
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        v0, f0 = len(bm.verts), len(bm.faces)
        bm.from_mesh(tmp_me)
    finally:
        bpy.data.meshes.remove(tmp_me)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    new_verts = bm.verts[v0:]
    created = bm.faces[f0:]
    edge_faces = created[n_quads:]
    _FACE_TIMINGS["assemble_ms"] = _FACE_TIMINGS.get("assemble_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    _t0 = time.perf_counter()
    # Close wrap-around seams and swallow slivers along the outline (one C pass; the
    # distance stays well below the closest grid-line spacing).
    gaps = [b - a for a, b in zip(UL, UL[1:])] + [b - a for a, b in zip(VL, VL[1:])]
    weld = min(size * 1e-4, 0.1 * min(gaps)) if gaps else size * 1e-4
    bmesh.ops.remove_doubles(bm, verts=list(new_verts), dist=max(weld, size * 1e-7))
    loose = [v for v in new_verts if v.is_valid and not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
    if p.mesh_type == 'TRIS':
        bmesh.ops.triangulate(bm, faces=[f for f in created if f.is_valid],
                              quad_method='BEAUTY', ngon_method='BEAUTY')
    else:
        ngons = [f for f in edge_faces if f.is_valid and len(f.verts) > 4]
        if ngons:
            bmesh.ops.triangulate(bm, faces=ngons, quad_method='BEAUTY', ngon_method='BEAUTY')
    _FACE_TIMINGS["cleanup_ms"] = _FACE_TIMINGS.get("cleanup_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    return True


def _smooth_face_source(tmp, p):
    """Subdivide and soften internal face transitions while preserving the outer boundary.

    Smoothing is done on the source surface before the pleat profile is applied, so
    the pleat peaks/valleys are not blurred by a generic post-process on the result.
    Selection boundaries are held in place to keep the source outline predictable.
    """
    if not getattr(p, "face_smooth", True) or p.face_smooth_factor <= 1e-6 or not tmp.faces:
        return

    edges = list(tmp.edges)
    if not edges:
        return

    cuts = max(1, min(4, int(p.face_smooth_cuts)))
    try:
        bmesh.ops.subdivide_edges(
            tmp, edges=edges, cuts=cuts, use_grid_fill=True,
            use_single_edge=False, use_only_quads=False)
    except TypeError:
        # Compatibility fallback for Blender builds whose BMesh signature differs.
        bmesh.ops.subdivide_edges(tmp, edges=edges, cuts=cuts, use_grid_fill=True)

    tmp.verts.ensure_lookup_table()
    tmp.edges.ensure_lookup_table()
    tmp.faces.ensure_lookup_table()

    # Outline handling: interior points are smoothed in 3D. Outline points slide
    # along the outline (1-D smoothing), so where the outline crosses a crease
    # between panels it is rounded exactly like the surface next to it (no pinched
    # hourglass corners). Corners of a flat face stay sharp: the outline keeps its shape.
    tmp.normal_update()
    boundary = {v for e in tmp.edges if len(e.link_faces) == 1 for v in e.verts}
    interior = [v for v in tmp.verts if v not in boundary]
    slide = []
    for v in boundary:
        nb = [e.other_vert(v) for e in v.link_edges if len(e.link_faces) == 1]
        if len(nb) != 2:
            continue
        a, b = (v.co - nb[0].co), (nb[1].co - v.co)
        turn = a.length > 1e-12 and b.length > 1e-12 and a.normalized().dot(b.normalized()) < 0.985
        normals = [f.normal for f in v.link_faces if f.normal.length > 1e-12]
        flat = all(abs(normals[0].dot(n)) > 0.985 for n in normals[1:]) if normals else True
        if turn and flat:
            continue                                   # a real corner of a flat face
        slide.append((v, nb[0], nb[1]))
    fac = float(p.face_smooth_factor)
    # Outline points copy the movement of the row just inside them, so a rounded
    # crease is rounded by the same amount all the way to the edge (no barrel).
    inner_nbrs = {v: [e.other_vert(v) for e in v.link_edges if e.other_vert(v) not in boundary]
                  for v, _a, _b in slide}
    for _ in range(2):
        if interior:
            before = {v: v.co.copy() for v in interior}
            bmesh.ops.smooth_vert(tmp, verts=interior, factor=fac,
                                  use_axis_x=True, use_axis_y=True, use_axis_z=True)
            for v, _n0, _n1 in slide:
                nb = inner_nbrs[v]
                if nb:
                    d = Vector((0.0, 0.0, 0.0))
                    for w in nb:
                        d += w.co - before[w]
                    v.co += d / len(nb)

    tmp.verts.index_update()
    tmp.edges.index_update()
    tmp.faces.index_update()
    comps = _face_components(tmp)
    for comp in comps:
        if len(comp) > 1:
            bmesh.ops.recalc_face_normals(tmp, faces=comp)
    tmp.normal_update()


def _faces_bmesh(src, smooth_p=None):
    tmp = bmesh.new()
    tv = [tmp.verts.new(v) for v in src["verts"]]
    for f in src["faces"]:
        try:
            tmp.faces.new([tv[i] for i in f])
        except ValueError:
            pass
    tmp.verts.index_update()
    tmp.edges.index_update()
    tmp.faces.index_update()
    if smooth_p is not None:
        _smooth_face_source(tmp, smooth_p)
    comps = _face_components(tmp)
    for comp in comps:
        if len(comp) > 1:
            bmesh.ops.recalc_face_normals(tmp, faces=comp)
    tmp.normal_update()
    return tmp, comps


_FACE_TIMINGS = {}


def build_face(bm, p, src, ref=None):
    _FACE_TIMINGS.clear()
    _t0 = time.perf_counter()
    tmp, comps = _faces_bmesh(src, smooth_p=p)
    _FACE_TIMINGS["source_ms"] = (time.perf_counter() - _t0) * 1000.0
    try:
        for comp in comps:
            _face_component(bm, p, comp)
        if ref is not None:
            vm = {}
            for f in tmp.faces:
                vl = []
                for v in f.verts:
                    if v not in vm:
                        vm[v] = ref.verts.new(v.co)
                    vl.append(vm[v])
                try:
                    ref.faces.new(vl)
                except ValueError:
                    pass
    finally:
        tmp.free()


def apply_shading(bm, p):
    smooth = p.smooth or p.auto_smooth
    for f in bm.faces:
        f.smooth = smooth
    for e in bm.edges:
        sharp = p.auto_smooth and len(e.link_faces) == 2 and e.calc_face_angle(0.0) > p.auto_smooth_angle
        e.smooth = not sharp


def apply_border_shading(bm, p):
    """Smooth-shade the separately generated frame and retain hard edges by angle."""
    smooth = p.border_smooth or p.border_auto_smooth
    for face in bm.faces:
        face.smooth = smooth
    for edge in bm.edges:
        sharp = (p.border_auto_smooth and len(edge.link_faces) == 2
                 and edge.calc_face_angle(0.0) > p.border_auto_smooth_angle)
        edge.smooth = not sharp


# ---------------------------------------------------------------------------
# Border
# ---------------------------------------------------------------------------


def _border_dims(p, small=None):
    W = max(p.border_width, 1e-6)
    if small:
        W = min(W, small * 0.48)
    # A seal past the reference edge keeps the pleat ends inside the frame. The shell
    # thickness can push the last pleat slightly past the edge, so the seal covers it.
    effective_thickness = p.thickness if getattr(p, "use_thickness", False) else 0.0
    O = max(5e-4, 0.03 * W) + effective_thickness
    fit = p.pleat_depth + effective_thickness
    F = B = fit + W / 3.0               # the frame stands a third of its width above the pleats
    return W, O, F, B, fit


def _border_section(p, W, O, F, B, fit=0.0):
    pts = [(-O, -B), (W, -B), (W, F), (-O, F)]
    out = []
    for q in pts:
        if not out or abs(q[0] - out[-1][0]) + abs(q[1] - out[-1][1]) > 1e-9:
            out.append(q)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) + abs(out[0][1] - out[-1][1]) <= 1e-9:
        out.pop()
    return out


def _untangle_offsets(co, P):
    """Offset points of a closed loop (one section point per station). Where the loop has
    stations closer to a corner than the offset distance, the offset edge folds back on
    itself and the frame turns inside out there (gaps and clipping at the corners). Every
    segment that runs against the loop direction is collapsed onto the sharper corner."""
    n = len(co)
    if n < 3:
        return P
    turn = []
    for k in range(n):
        a = co[k] - co[k - 1]
        b = co[(k + 1) % n] - co[k]
        if a.length < 1e-12 or b.length < 1e-12:
            turn.append(0.0)
        else:
            turn.append(1.0 - a.normalized().dot(b.normalized()))
    P = [q.copy() for q in P]
    for _it in range(n):
        changed = False
        for k in range(n):
            k2 = (k + 1) % n
            base = co[k2] - co[k]
            seg = P[k2] - P[k]
            if base.length < 1e-12 or seg.length < 1e-12:
                continue
            if seg.dot(base) < 0.0:
                src, dst = (k, k2) if turn[k] >= turn[k2] else (k2, k)
                P[dst] = P[src].copy()
                turn[dst] = turn[src]
                changed = True
        if not changed:
            break
    return P


def _sweep_loop(co, nrm, inw_edge, sec, face_n=None):
    n, m_ = len(co), len(sec)
    miters, hvs = [], []
    for k in range(n):
        a, b = inw_edge[k - 1], inw_edge[k]
        m = a + b
        if m.length < 1e-6:
            m = b.copy()
        if m.length > 1e-9:
            m.normalize()
        c = max(m.dot(b), 0.3)
        miters.append(m / c)
        # Where the surface folds (box corners, L folds) the vertex normal is the average
        # of the two faces; offsetting along it would make the frame thinner than F / B
        # measured from each face, and the pleats poked through. Stretch it so the
        # frame keeps its full height above both faces.
        hv = nrm[k]
        if face_n is not None:
            dots = [abs(hv.dot(fn)) for fn in (face_n[k - 1], face_n[k]) if fn.length > 1e-9]
            if dots:
                hv = hv / max(min(dots), 0.3)
        hvs.append(hv)
    cols = []
    for s, h in sec:
        P = [co[k] + miters[k] * s + hvs[k] * h for k in range(n)]
        if abs(s) > 1e-12:
            P = _untangle_offsets(co, P)
        cols.append(P)
    verts = []
    for k in range(n):
        for j in range(m_):
            verts.append(cols[j][k])
    # UVs in metres: length along the loop x length around the cross-section
    ucum = [0.0]
    for k in range(1, n):
        ucum.append(ucum[-1] + (co[k] - co[k - 1]).length)
    u_end = ucum[-1] + (co[0] - co[-1]).length
    vcum = [0.0]
    for j in range(1, m_):
        vcum.append(vcum[-1] + math.hypot(sec[j][0] - sec[j - 1][0], sec[j][1] - sec[j - 1][1]))
    v_end = vcum[-1] + math.hypot(sec[0][0] - sec[-1][0], sec[0][1] - sec[-1][1])
    quads, uvs = [], []
    for k in range(n):
        k2 = (k + 1) % n
        ua, ub = ucum[k], (ucum[k2] if k2 > k else u_end)
        for j in range(m_):
            j2 = (j + 1) % m_
            va, vb = vcum[j], (vcum[j2] if j2 > j else v_end)
            quads.append((k * m_ + j, k2 * m_ + j, k2 * m_ + j2, k * m_ + j2))
            uvs.append(((ua, va), (ub, va), (ub, vb), (ua, vb)))
    return verts, quads, uvs


def build_border(p, rb):
    rb.verts.ensure_lookup_table()
    rb.edges.ensure_lookup_table()
    rb.faces.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    bedges = [(e.verts[0].index, e.verts[1].index) for e in rb.edges if len(e.link_faces) == 1]
    if not bedges:
        return None
    small = _rb_small(rb)
    W, O, F, B, _fit = _border_dims(p, small)
    sec = _border_section(p, W, O, F, B, _fit)
    if len(sec) < 3:
        return None
    fb = bmesh.new()
    loops_out = []
    for ids, cl in _chains(bedges):
        if not cl or len(ids) < 3:
            continue
        P = [rb.verts[i] for i in ids]
        n = len(P)
        inw, fns = [], []
        for k in range(n):
            a, b = P[k], P[(k + 1) % n]
            e = rb.edges.get((a, b))
            dk = b.co - a.co
            if e is None or not e.link_faces or dk.length < 1e-12:
                inw.append(Vector((0, 0, 0)))
                fns.append(Vector((0, 0, 0)))
                continue
            f = e.link_faces[0]
            fns.append(f.normal.copy())
            iv = f.normal.cross(dk.normalized())
            if iv.length < 1e-9:
                inw.append(Vector((0, 0, 0)))
                continue
            iv.normalize()
            if iv.dot(f.calc_center_median() - (a.co + b.co) * 0.5) < 0:
                iv = -iv
            inw.append(iv)
        nrm = [v.normal.copy() if v.normal.length > 1e-9 else Vector((0, 0, 1)) for v in P]
        verts, quads, quvs = _sweep_loop([v.co for v in P], nrm, inw, sec, fns)
        fv = [fb.verts.new(c) for c in verts]
        uvl = fb.loops.layers.uv.get(UV_LAYER) or fb.loops.layers.uv.new(UV_LAYER)
        for q, quv in zip(quads, quvs):
            try:
                f = fb.faces.new([fv[i] for i in q])
            except ValueError:
                continue
            for lp, uv in zip(f.loops, quv):
                lp[uvl].uv = uv
        loops_out.append((fv, len(sec), sec))
    if not fb.faces:
        fb.free()
        return None
    bmesh.ops.recalc_face_normals(fb, faces=fb.faces[:])
    return fb


# ---------------------------------------------------------------------------
# Boolean cutter
# ---------------------------------------------------------------------------


# ---- printed graphics ---------------------------------------------------------


def _cut_pad(p):
    effective_thickness = p.thickness if getattr(p, "use_thickness", False) else 0.0
    return p.pleat_depth + effective_thickness + 1e-5


def _newell(vs):
    n = Vector((0, 0, 0))
    for i in range(len(vs)):
        a, b = vs[i], vs[(i + 1) % len(vs)]
        n += Vector(((a.y - b.y) * (a.z + b.z), (a.z - b.z) * (a.x + b.x), (a.x - b.x) * (a.y + b.y)))
    return n


def _frame_face(p, src):
    vs = [Vector(v) for v in src["verts"]]
    best, bn = -1.0, Vector((0, 0, 1))
    for f in src["faces"]:
        n = _newell([vs[i] for i in f])
        if n.length > best:
            best, bn = n.length, n
    n0 = bn.normalized() if bn.length > 1e-12 else Vector((0, 0, 1))
    ref = Vector((1, 0, 0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0, 1, 0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0)
    a = p.pleat_angle
    ua = u0 * math.cos(a) + v0 * math.sin(a)
    ra = n0.cross(ua)
    return [ua, ra, n0], vs, 2


def _frame_curve(p, src):
    d, H = _height_dir(p), max(p.height, 1e-5)
    pts = [Vector(x) for path in src["paths"] for x in path]
    longest, bl = None, -1.0
    for path, cl in zip(src["paths"], src["closed"]):
        pp = [Vector(x) for x in path]
        L = _polyline_length(pp, cl)
        if L > bl:
            bl, longest = L, pp
    if not longest:
        raise ValueError("No valid source path found for the boolean cutter.")
    t = longest[-1] - longest[0]
    t = t - d * t.dot(d)
    if t.length < 1e-6 * max(bl, 1e-9):
        t = Vector((1, 0, 0)) - d * d.x
        if t.length < 1e-6:
            t = Vector((0, 1, 0)) - d * d.y
    a1 = t.normalized()
    a3 = a1.cross(d)
    if a3.length < 1e-9:
        a3 = d.cross(Vector((1, 0, 0)))
    a3.normalize()
    return [a1, d, a3], pts, 2, H


def _box_geometry(ax, rng):
    verts = []
    for i in (0, 1):
        for j in (0, 1):
            for k in (0, 1):
                verts.append(ax[0] * rng[0][i] + ax[1] * rng[1][j] + ax[2] * rng[2][k])
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return verts, quads


def _box_ranges(p, ax, pts, thick_axis, extra_hi=None):
    rng, pad, margin = [], _cut_pad(p), p.cutter_margin
    for i in range(3):
        cs = [q.dot(ax[i]) for q in pts]
        lo, hi = min(cs), max(cs)
        if extra_hi is not None and i == extra_hi[0]:
            hi += extra_hi[1]
        if i == thick_axis:
            lo, hi = lo - pad, hi + pad
        else:
            lo, hi = lo - margin, hi + margin
            if hi - lo < 1e-6:
                mid = (hi + lo) * 0.5
                lo, hi = mid - 5e-4, mid + 5e-4
        rng.append((lo, hi))
    return rng


def _make_keep(p, ax, rng):
    eps, op = 1e-6, p.boolean_op
    def keep(pt):
        inside = all(rng[i][0] - eps <= pt.dot(ax[i]) <= rng[i][1] + eps for i in range(3))
        if op == 'INTERSECT':
            return inside
        if op == 'DIFFERENCE':
            return not inside
        return True
    return keep


def _slab(src_bm, pad):
    src_bm.verts.index_update()
    src_bm.normal_update()
    out, top, bot = bmesh.new(), {}, {}
    for v in src_bm.verts:
        n = v.normal.copy()
        if n.length < 1e-9:
            n = Vector((0, 0, 1))
        top[v] = out.verts.new(v.co + n * pad)
        bot[v] = out.verts.new(v.co - n * pad)
    def mk(vs):
        try:
            out.faces.new(vs)
        except ValueError:
            pass
    for f in src_bm.faces:
        vl = list(f.verts)
        mk([top[v] for v in vl])
        mk([bot[v] for v in reversed(vl)])
    for e in src_bm.edges:
        if len(e.link_faces) == 1:
            a, b = e.verts
            mk([bot[a], bot[b], top[b], top[a]])
    bmesh.ops.recalc_face_normals(out, faces=out.faces[:])
    return out


def build_cutter(p, src):
    if p.boolean_source == 'ENVELOPE':
        pad = _cut_pad(p)
        if p.mode == 'CURVE':
            rb = bmesh.new()
            try:
                d, H = _height_dir(p), max(p.height, 1e-5)
                for path, cl in zip(src["paths"], src["closed"]):
                    _wall_path(rb, [Vector(x) for x in path], cl, d, H, p.pleat_density)
                out = _slab(rb, pad)
            finally:
                rb.free()
        else:
            tmp, _comps = _faces_bmesh(src)
            try:
                out = _slab(tmp, pad)
            finally:
                tmp.free()
        return out, None
    if p.mode == 'CURVE':
        ax, pts, tk, H = _frame_curve(p, src)
        rng = _box_ranges(p, ax, pts, tk, extra_hi=(1, H))
    else:
        ax, pts, tk = _frame_face(p, src)
        rng = _box_ranges(p, ax, pts, tk)
    cb = bmesh.new()
    verts, quads = _box_geometry(ax, rng)
    vs = [cb.verts.new(c) for c in verts]
    for q in quads:
        try:
            cb.faces.new([vs[i] for i in q])
        except ValueError:
            pass
    bmesh.ops.recalc_face_normals(cb, faces=cb.faces[:])
    return cb, _make_keep(p, ax, rng)


# ---------------------------------------------------------------------------
# Regeneration / object children
# ---------------------------------------------------------------------------

CHILD_ATTRS = ("border_obj", "cutter_obj")


_CHILD_SUFFIX = {
    "border_obj": "_Border",
    "cutter_obj": "_Cutter",
}


def _find_child(obj, attr):
    """Find an existing generated child even if its PointerProperty was lost."""
    suffix = _CHILD_SUFFIX.get(attr)
    if obj is None or not suffix:
        return None
    pattern = re.compile(re.escape(suffix) + r"(?:\.\d{3})?$")
    for child in obj.children:
        if child.type in {'MESH', 'CURVE'} and pattern.search(child.name):
            return child
    return None


def _remove_child(p, attr):
    ob = getattr(p, attr, None)
    owner = getattr(p, "id_data", None)
    if ob is None and isinstance(owner, bpy.types.Object):
        ob = _find_child(owner, attr)
    if ob:
        data = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)
    try:
        setattr(p, attr, None)
    except Exception:
        pass


def _write_child(obj, p, attr, suffix, cbm, mat):
    ob = getattr(p, attr, None)
    if ob is None or ob.name not in bpy.data.objects or ob.parent != obj:
        ob = _find_child(obj, attr)
    if ob is not None and ob.type != 'MESH':
        _remove_child(p, attr)
        ob = None
    if ob is None:
        name = obj.name + suffix
        me = bpy.data.meshes.new(name)
        ob = bpy.data.objects.new(name, me)
        cols = obj.users_collection
        (cols[0] if cols else bpy.context.scene.collection).objects.link(ob)
        ob.parent = obj
    setattr(p, attr, ob)
    me = ob.data
    me.clear_geometry()
    cbm.to_mesh(me)
    me.materials.clear()
    if mat:
        me.materials.append(mat)
    me.update()
    return ob


def _style_cutter(p):
    co = p.cutter_obj
    if co is None:
        return
    co.display_type = 'WIRE'
    co.hide_render = True
    try:
        co.hide_set(not p.show_cutter)
    except Exception:
        pass


def _boolean_mod_name(filter_obj, p):
    name = getattr(p, "boolean_modifier_name", "")
    if not name:
        name = "%s [%s]" % (BOOL_NAME, filter_obj.name)
        try:
            p.boolean_modifier_name = name
        except Exception:
            pass
    return name


def _remove_source_boolean(filter_obj, p):
    source = getattr(p, "source_obj", None)
    if source is not None and source.type == 'MESH':
        name = _boolean_mod_name(filter_obj, p)
        mod = source.modifiers.get(name)
        if mod is not None:
            source.modifiers.remove(mod)
    # Clean up the old v3.5 behavior if this filter file was upgraded in-place.
    legacy = filter_obj.modifiers.get(BOOL_NAME)
    if legacy is not None and legacy.type == 'BOOLEAN':
        filter_obj.modifiers.remove(legacy)


def sync_boolean(obj, p):
    """Cut the original source mesh with the generated bounds, not the filter mesh."""
    source = getattr(p, "source_obj", None)
    host = source if source is not None and source.type == 'MESH' else None
    target = p.boolean_obj if p.boolean_source == 'OBJECT' else p.cutter_obj
    name = _boolean_mod_name(obj, p)
    mod = host.modifiers.get(name) if host is not None else None

    # Remove any legacy modifier previously placed on the generated filter itself.
    legacy = obj.modifiers.get(BOOL_NAME)
    if legacy is not None and legacy.type == 'BOOLEAN':
        obj.modifiers.remove(legacy)

    valid = (p.use_boolean and host is not None and target is not None and target != host
             and target != obj and target.name in bpy.data.objects and target.type == 'MESH')
    if not valid:
        if mod is not None:
            host.modifiers.remove(mod)
        return

    if mod is None:
        mod = host.modifiers.new(name, 'BOOLEAN')
    mod.operation = p.boolean_op
    mod.solver = p.boolean_solver
    try:
        mod.operand_type = 'OBJECT'
    except Exception:
        pass
    mod.object = target

    # The source may have been hidden by the source-generation operator.
    try:
        host.hide_set(False)
    except Exception:
        pass


# ---- border coverage -------------------------------------------------------


def _rb_small(rb):
    """Narrowest width of the reference surface: twice the largest distance from any of
    its points to the outline. (A world-space bounding box underestimates curved panels:
    a cylinder section is shallow along one axis but wide across its surface.)"""
    xs = [v.co.x for v in rb.verts]
    ys = [v.co.y for v in rb.verts]
    zs = [v.co.z for v in rb.verts]
    if not xs:
        return None
    ext = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    mx = max(ext)
    nz = [e for e in ext if e > 1e-6 * max(mx, 1e-9)]
    bbox_small = min(nz) if nz else mx
    try:
        segs = _outline_segments(rb, max(mx, 1e-9) * 1e-4)
        if segs:
            inner = max(_outline_distance([tuple(v.co) for v in rb.verts], segs))
            if 1e-9 < inner < 1e8:
                return max(bbox_small, 2.0 * inner)
    except Exception:
        traceback.print_exc()
    return bbox_small


def _dp_keep(pts, tol):
    """Douglas-Peucker: indices of the polyline points needed within tolerance."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        pa, pb = pts[a], pts[b]
        ab = pb - pa
        ll = ab.length_squared
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            q = pts[i] - pa
            if ll > 1e-20:
                t = max(0.0, min(1.0, q.dot(ab) / ll))
                d2 = (q - ab * t).length_squared
            else:
                d2 = q.length_squared
            if d2 > best:
                best, idx = d2, i
        if best > tol2:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [i for i in range(n) if keep[i]]


def _outline_segments(rb, tol):
    """Simplified outline of the reference surface as (start, end, normal) triples."""
    rb.verts.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    pairs, edge_n = [], {}
    for e in rb.edges:
        if len(e.link_faces) == 1:
            a, b = e.verts[0].index, e.verts[1].index
            pairs.append((a, b))
            edge_n[(min(a, b), max(a, b))] = e.link_faces[0].normal.copy()
    cos = [v.co.copy() for v in rb.verts]
    segs = []
    for ids, closed in _chains(pairs):
        if closed:
            ids = ids + [ids[0]]
        pts = [cos[i] for i in ids]
        keep = _dp_keep(pts, tol)
        for a, b in zip(keep, keep[1:]):
            i0, i1 = ids[a], ids[a + 1]
            n = edge_n.get((min(i0, i1), max(i0, i1)), Vector((0.0, 0.0, 1.0)))
            segs.append((pts[a], pts[b], n))
    return segs


def _outline_distance(points, segs):
    """In-plane distance from each point to the outline (pleat depth is ignored)."""
    if not segs:
        return [1e9] * len(points)
    if np is not None:
        P0 = np.array([tuple(s[0]) for s in segs], dtype=np.float64)
        P1 = np.array([tuple(s[1]) for s in segs], dtype=np.float64)
        NN = np.array([tuple(s[2]) for s in segs], dtype=np.float64)
        D = P1 - P0
        LL = np.maximum((D * D).sum(1), 1e-20)
        pts = np.array(points, dtype=np.float64).reshape(-1, 3)
        out = np.empty(len(pts))
        chunk = max(1, 300000 // len(segs))
        for s in range(0, len(pts), chunk):
            Q = pts[s:s + chunk, None, :]
            t = np.clip(((Q - P0[None]) * D[None]).sum(2) / LL[None], 0.0, 1.0)
            V = Q - (P0[None] + t[..., None] * D[None])
            V = V - (V * NN[None]).sum(2)[..., None] * NN[None]
            out[s:s + chunk] = np.sqrt((V * V).sum(2)).min(1)
        return out.tolist()
    out = []
    for q in points:
        q = Vector(q)
        best = 1e18
        for a, b, n in segs:
            ab = b - a
            ll = ab.length_squared
            t = 0.0 if ll < 1e-20 else max(0.0, min(1.0, (q - a).dot(ab) / ll))
            d = q - (a + ab * t)
            d = d - n * d.dot(n)
            best = min(best, d.length_squared)
        out.append(math.sqrt(best))
    return out


# ---- generated instance collections --------------------------------------


# ---- emitter object and the two systems -----------------------------------


def _apply_uv_space(me, p):
    """Turn the raw UVs (metres) into the chosen UV space."""
    uvl = me.uv_layers.get(UV_LAYER) if me is not None else None
    if uvl is None or not len(uvl.data) or np is None:
        return
    uv = np.empty(len(uvl.data) * 2, dtype=np.float32)
    uvl.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    if getattr(p, "uv_mode", 'WORLD') == 'FIT':
        lo = uv.min(axis=0)
        span = max(float((uv.max(axis=0) - lo).max()), 1e-9)
        uv = (uv - lo) / span
    else:
        uv = uv / max(getattr(p, "uv_tile", 0.1), 1e-6)
    uvl.data.foreach_set("uv", uv.astype(np.float32).ravel())


# ---- the pleated sheet, cached between updates ------------------------------

LAST_TIMINGS = {}
_SHEET_KEYS = ("mode", "pleat_depth", "pleat_density", "pleat_phase", "detail", "height",
               "height_dir", "pleat_angle", "mesh_type", "face_smooth", "face_smooth_factor",
               "face_smooth_cuts", "bevel_ridge", "bevel_valley", "bevel_cuts")


def _sheet_key(p):
    vals = [p.src]
    for k in _SHEET_KEYS:
        v = getattr(p, k)
        vals.append([round(float(x), 9) for x in v] if k == "height_dir" else
                    (round(v, 9) if isinstance(v, float) else v))
    return hashlib.md5(json.dumps(vals).encode("utf-8")).hexdigest()


def _build_sheet(obj, p, src, bm):
    """Pleated sheet (before thickness). Re-uses the cached copy when nothing that
    shapes the pleats has changed, so the other edits skip the expensive rebuild."""
    key = _sheet_key(p)
    name = obj.get("fm_sheet_cache")
    cm = bpy.data.meshes.get(name) if name else None
    if cm is not None and cm.get("fm_key") == key and len(cm.polygons):
        bm.from_mesh(cm)
        return True
    if p.mode == 'CURVE':
        build_curve(bm, p, src, None)
        if p.mesh_type == 'TRIS' and bm.faces:
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
    else:
        build_face(bm, p, src, None)
    if cm is None:
        cm = bpy.data.meshes.new(".FM sheet " + obj.name)
        obj["fm_sheet_cache"] = cm.name
    cm.clear_geometry()
    bm.to_mesh(cm)
    cm["fm_key"] = key
    return False


def build_reference(rb, p, src):
    """Flat reference surface (the outline the border follows)."""
    if p.mode == 'CURVE':
        d, H = _height_dir(p), max(p.height, 1e-5)
        for path, cl in zip(src["paths"], src["closed"]):
            _wall_path(rb, [Vector(x) for x in path], cl, d, H, p.pleat_density)
        return
    tmp, _comps = _faces_bmesh(src, smooth_p=p)
    try:
        vm = {}
        for f in tmp.faces:
            vl = []
            for v in f.verts:
                if v not in vm:
                    vm[v] = rb.verts.new(v.co)
                vl.append(vm[v])
            try:
                rb.faces.new(vl)
            except ValueError:
                pass
    finally:
        tmp.free()


def regenerate(obj):
    p = getattr(obj, "filter_maker", None)
    if p is None or not p.is_filter or obj.mode != 'OBJECT':
        return
    p.last_error = ""
    bm = rb = None
    tm = {}
    t_start = t_last = time.perf_counter()

    def tick(name):
        nonlocal t_last
        now = time.perf_counter()
        tm[name] = round((now - t_last) * 1000.0, 2)
        t_last = now

    try:
        src = json.loads(p.src)
        bm = bmesh.new()
        tm["sheet_cached"] = _build_sheet(obj, p, src, bm)
        tick("sheet_ms")
        if not tm["sheet_cached"] and p.mode == 'FACE':
            tm.update({"face_" + k: round(v, 1) for k, v in _FACE_TIMINGS.items()})
        if p.use_border:
            rb = bmesh.new()
            build_reference(rb, p, src)
        tick("reference_ms")
        bm.normal_update()
        if p.use_thickness and p.thickness > 1e-7 and bm.faces:
            bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=p.thickness)
        bm.normal_update()
        tick("thickness_ms")
        if p.use_boolean and p.boolean_source != 'OBJECT':
            cb, _keep = build_cutter(p, src)
            try:
                _write_child(obj, p, "cutter_obj", "_Cutter", cb, None)
            finally:
                cb.free()
            _style_cutter(p)
        else:
            _remove_child(p, "cutter_obj")
        tick("cutter_ms")
        if rb is not None and rb.faces and p.use_border:
            fb = build_border(p, rb)
            if fb is not None:
                try:
                    apply_border_shading(fb, p)
                    bob = _write_child(obj, p, "border_obj", "_Border", fb, p.border_material)
                    _apply_uv_space(bob.data, p)
                finally:
                    fb.free()
            else:
                _remove_child(p, "border_obj")
        else:
            _remove_child(p, "border_obj")
        tick("border_ms")
        apply_shading(bm, p)
        me = obj.data
        me.clear_geometry()
        bm.to_mesh(me)
        _apply_uv_space(me, p)
        me.materials.clear()
        if p.filter_material:
            me.materials.append(p.filter_material)
        if hasattr(me, "use_auto_smooth"):
            try:
                me.use_auto_smooth = bool(p.auto_smooth)
                if p.auto_smooth:
                    me.auto_smooth_angle = p.auto_smooth_angle
            except Exception:
                pass
        me.update()
        sync_boolean(obj, p)
        tick("write_ms")
    except Exception as exc:
        traceback.print_exc()
        try:
            p.last_error = str(exc)
        except Exception:
            pass
    finally:
        if bm is not None:
            bm.free()
        if rb is not None:
            rb.free()
        tm["total_ms"] = round((time.perf_counter() - t_start) * 1000.0, 2)
        LAST_TIMINGS.clear()
        LAST_TIMINGS.update(tm)
        redraw_all()


def refresh_curve_source(filter_obj):
    """Refresh a generated filter's stored path from its linked editable curve."""
    p = getattr(filter_obj, "filter_maker", None)
    if p is None or p.mode != 'CURVE':
        return False
    source = getattr(p, "source_obj", None)
    if source is None or source.type != 'CURVE':
        return False
    try:
        new_src = paths_from_curve(source)
        try:
            old_src = json.loads(p.src) if p.src else None
        except Exception:
            old_src = None
        if new_src == old_src:
            return False
        p.src = json.dumps(new_src)
        return True
    except Exception:
        traceback.print_exc()
        return False


def _upd(self, context):
    if _busy[0] or not self.is_filter or not self.live:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        regenerate(ob)


def _upd_thickness_enabled(self, context):
    """Enable thickness with a useful starting amount instead of a zero-width shell."""
    if _busy[0]:
        return
    if getattr(self, "use_thickness", False) and getattr(self, "thickness", 0.0) <= 1e-8:
        scale = 1.0
        try:
            if self.src:
                scale = _src_size(self.mode, json.loads(self.src))
        except Exception:
            scale = 1.0
        _busy[0] = True
        try:
            self.thickness = 0.004 * max(scale, 1e-3)
        finally:
            _busy[0] = False
    _upd(self, context)


def _upd_bool(self, context):
    if _busy[0] or not self.is_filter:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        regenerate(ob)


def _upd_cutter_vis(self, context):
    _style_cutter(self)


def _poll_mesh(self, obj):
    try:
        return obj.type == 'MESH' and obj != self.id_data and not getattr(getattr(obj, "filter_maker", None), "is_filter", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _len(name, default, desc, mn=0.0, upd=None):
    return FloatProperty(name=name, default=default, min=mn, description=desc,
                         subtype='DISTANCE', unit='LENGTH', precision=4, update=upd or _upd)


class FilterProps(PropertyGroup):
    is_filter: BoolProperty(default=False)
    mode: EnumProperty(items=[('CURVE', "Curve / Edges", ""), ('FACE', "Faces", "")], default='CURVE')
    src: StringProperty(options={'HIDDEN'})
    live: BoolProperty(name="Live Update", default=True,
                       description="Regenerate while dragging sliders (turn off for heavy filters)")
    last_error: StringProperty(options={'HIDDEN'})
    source_obj: PointerProperty(type=bpy.types.Object)

    # Pleats
    use_thickness: BoolProperty(name="Use Thickness", default=False, update=_upd_thickness_enabled,
                                description="Give the filter media a solid shell; off = zero thickness")
    thickness: _len("Thickness", 0.0, "Thickness of the filter media", 0.0)
    pleat_depth: _len("Pleat Depth", 0.025, "Distance from centre line to each ridge / valley")
    pleat_density: FloatProperty(name="Pleat Density", default=55.0, min=0.05, soft_max=200.0,
                                 description="Pleats (one ridge + one valley) per unit length", update=_upd)
    pleat_phase: FloatProperty(name="Pleat Offset", default=0.0, min=0.0, max=1.0,
                               description="Slide the pleat pattern by a fraction of one pleat", update=_upd)
    detail: FloatProperty(name="Surface Detail", default=20.0, min=0.05, soft_max=200.0,
                          description="Cuts per unit length", update=_upd)
    height: _len("Height", 1.0, "Extrusion height", 0.001)
    height_dir: FloatVectorProperty(name="Height Direction", size=3, default=(0, 0, 1),
                                    subtype='XYZ', update=_upd,
                                    description="Direction the pleated sheet is extruded (X, Y, Z)")
    pleat_angle: FloatProperty(name="Pleat Angle", default=0.0, subtype='ANGLE', unit='ROTATION',
                               description="Rotate pleat direction on faces", update=_upd)
    mesh_type: EnumProperty(name="Topology", items=[
        ('QUADS', "Quads", "Quad topology; split n-gons only where needed"),
        ('TRIS', "Triangles", "Fully triangulated mesh")], default='QUADS', update=_upd)
    face_smooth: BoolProperty(
        name="Smooth Face Transitions", default=True, update=_upd,
        description="Subdivide and soften internal transitions in connected face selections before pleating")
    face_smooth_factor: FloatProperty(
        name="Smoothing", default=0.45, min=0.0, max=1.0, soft_max=0.8, update=_upd,
        description="Strength of the source-surface smoothing; outer selection boundary stays fixed")
    face_smooth_cuts: IntProperty(
        name="Smoothing Cuts", default=2, min=1, max=4, update=_upd,
        description="Extra source-surface subdivisions used to round faceted transitions")

    # Pleat bevel
    bevel_ridge: FloatProperty(name="Ridge Bevel", default=0.4, min=0, max=1, update=_upd)
    bevel_valley: FloatProperty(name="Valley Bevel", default=0.4, min=0, max=1, update=_upd)
    bevel_cuts: IntProperty(name="Bevel Cuts", default=3, min=1, max=16, update=_upd)

    # Shading
    smooth: BoolProperty(name="Smooth Shading", default=False, update=_upd)
    auto_smooth: BoolProperty(name="Auto Smooth", default=True, update=_upd)
    auto_smooth_angle: FloatProperty(name="Angle", default=math.radians(35), min=0, max=math.pi,
                                     subtype='ANGLE', unit='ROTATION', update=_upd)


    # Border
    use_border: BoolProperty(name="Border", default=False, update=_upd)
    border_width: _len("Width", 0.03, "Frame reach inward from reference edge", 0.0001)
    border_smooth: BoolProperty(name="Shade Smooth", default=False, update=_upd,
                                description="Enable smooth shading on the border frame")
    border_auto_smooth: BoolProperty(name="Auto Smooth", default=True, update=_upd,
                                     description="Keep frame edges sharp when their angle exceeds the threshold")
    border_auto_smooth_angle: FloatProperty(name="Angle", default=math.radians(35), min=0, max=math.pi,
                                            subtype='ANGLE', unit='ROTATION', update=_upd)


    # Boolean
    use_boolean: BoolProperty(name="Boolean", default=False, update=_upd_bool)
    boolean_source: EnumProperty(name="Cutter", items=[
        ('BOX', "Bounding Box", "A clean box around the reference selection"),
        ('ENVELOPE', "Envelope", "Reference shape thickened to cover the pleats"),
        ('OBJECT', "Object", "Any mesh object")], default='ENVELOPE', update=_upd_bool)
    boolean_obj: PointerProperty(name="Object", type=bpy.types.Object, poll=_poll_mesh, update=_upd_bool)
    boolean_modifier_name: StringProperty(options={'HIDDEN'})
    boolean_op: EnumProperty(name="Operation", items=[
        ('INTERSECT', "Intersect", "Keep only filter inside cutter"),
        ('DIFFERENCE', "Difference", "Cut cutter out of filter"), ('UNION', "Union", "Merge with cutter")],
        default='DIFFERENCE', update=_upd_bool)
    boolean_solver: EnumProperty(name="Solver", items=[('EXACT', "Exact", "More robust"), ('FAST', "Fast", "Quick preview")],
                                 default='EXACT', update=_upd_bool)
    cutter_margin: FloatProperty(name="Trim Margin", default=0.0, soft_min=-1, soft_max=1,
                                 subtype='DISTANCE', unit='LENGTH', precision=4, update=_upd_bool)
    show_cutter: BoolProperty(name="Show Cutter", default=False, update=_upd_cutter_vis)


    uv_mode: EnumProperty(name="UVs", items=[
        ('WORLD', "Real-World Size", "Textures tile at the same size on every filter, fold and frame"),
        ('FIT', "Fit 0-1", "Each part is scaled into the 0-1 UV square")], default='WORLD', update=_upd)
    uv_tile: _len("Texture Size", 0.1, "Real-world size of one texture tile (Real-World Size UVs)", 0.0001)

    # Materials / generated children
    filter_material: PointerProperty(name="Filter Material", type=bpy.types.Material, update=_upd)
    border_material: PointerProperty(name="Border Material", type=bpy.types.Material, update=_upd)
    border_obj: PointerProperty(type=bpy.types.Object)
    cutter_obj: PointerProperty(type=bpy.types.Object)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _default_mat(name, color):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.diffuse_color = color
    return m


def create_filter(context, mode, src, source=None):
    sc = _src_size(mode, src)
    me = bpy.data.meshes.new("Filter")
    ob = bpy.data.objects.new("Filter", me)
    (context.collection or context.scene.collection).objects.link(ob)
    _busy[0] = True
    try:
        p = ob.filter_maker
        p.is_filter = True
        p.mode = mode
        p.src = json.dumps(src)
        # sizes follow the source, so a small or a large source gets the same look
        p.pleat_depth, p.height, p.border_width = 0.025 * sc, 1.0 * sc, 0.03 * sc
        p.pleat_density, p.detail = 55.0 / sc, 20.0 / sc
        p.filter_material = _default_mat("Filter_Material", (0.85, 0.83, 0.75, 1.0))
        p.border_material = _default_mat("Border_Material", (0.25, 0.27, 0.3, 1.0))
        if source is not None:
            p.source_obj = source
            # The source mesh is the Boolean target, not the Object-mode cutter.
            # Leave boolean_obj empty until the user chooses a separate operand.
    finally:
        _busy[0] = False
    regenerate(ob)
    for o in context.view_layer.objects:
        if o is not None:
            o.select_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob
    redraw_all()
    return ob


class FILTER_OT_new_line(Operator):
    """Create an editable curve at the 3D cursor and build a live filter from it"""
    bl_idname = "filter.new_line"
    bl_label = "New Filter Line"
    bl_options = {'REGISTER', 'UNDO'}
    length: FloatProperty(name="Length", default=1.0, min=0.01, subtype='DISTANCE')

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        h = self.length / 2.0
        cu = bpy.data.curves.new("Filter_Line", 'CURVE')
        cu.dimensions = '3D'
        sp = cu.splines.new('BEZIER')
        sp.bezier_points.add(1)
        for bp, x in zip(sp.bezier_points, (-h, h)):
            bp.co = (x, 0.0, 0.0)
            bp.handle_left_type = bp.handle_right_type = 'FREE'
            bp.handle_left = (x - self.length / 6.0, 0.0, 0.0)
            bp.handle_right = (x + self.length / 6.0, 0.0, 0.0)
            bp.handle_left_type = bp.handle_right_type = 'AUTO'
        line = bpy.data.objects.new("Filter_Line", cu)
        line.location = context.scene.cursor.location
        (context.collection or context.scene.collection).objects.link(line)
        context.view_layer.update()
        src = paths_from_curve(line)
        if not src["paths"]:
            self.report({'ERROR'}, "Could not create the line curve.")
            return {'CANCELLED'}
        ob = create_filter(context, 'CURVE', src, source=line)
        if ob.filter_maker.last_error:
            self.report({'ERROR'}, ob.filter_maker.last_error)
        return {'FINISHED'}


class FILTER_OT_from_selection(Operator):
    """Build a filter from the active curve or mesh selection."""
    bl_idname = "filter.from_selection"
    bl_label = "Filter From Selection"
    bl_options = {'REGISTER', 'UNDO'}
    source: EnumProperty(name="Source", items=[('CURVE', "Curve / Edges", ""), ('FACE', "Faces", "")], default='CURVE')
    hide_source: BoolProperty(name="Hide Source", default=True)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type in {'MESH', 'CURVE'}

    def execute(self, context):
        obj = context.active_object
        try:
            if self.source == 'FACE':
                if obj.type != 'MESH':
                    self.report({'ERROR'}, "Faces mode needs a mesh.")
                    return {'CANCELLED'}
                src = faces_from_mesh(obj, selected_only=True)
                if not src["faces"]:
                    self.report({'ERROR'}, "Select one or more faces first (Edit Mode).")
                    return {'CANCELLED'}
            elif obj.type == 'CURVE':
                src = paths_from_curve(obj)
                if not src["paths"]:
                    self.report({'ERROR'}, "No usable curve splines found.")
                    return {'CANCELLED'}
            else:
                src = paths_from_mesh(obj, selected_only=True)
                if not src["paths"]:
                    self.report({'ERROR'}, "No edges found. Select edges in Edit Mode first.")
                    return {'CANCELLED'}
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, "Could not read source: %s" % exc)
            return {'CANCELLED'}
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        new = create_filter(context, self.source, src, source=obj)
        # Curves remain visible/editable as live guides. The hide option applies
        # only to mesh references, where curve-style editability is not available.
        if self.hide_source and not was_edit and obj.type == 'MESH':
            obj.hide_set(True)
        elif obj.type == 'CURVE':
            try:
                obj.hide_set(False)
            except Exception:
                pass
        if new.filter_maker.last_error:
            self.report({'ERROR'}, new.filter_maker.last_error)
        return {'FINISHED'}


class FILTER_OT_edit_source_curve(Operator):
    """Select the linked source curve and enter Edit Mode to shape the live filter."""
    bl_idname = "filter.edit_source_curve"
    bl_label = "Edit Source Curve"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = active_filter(context)
        return bool(ob and ob.filter_maker.source_obj and ob.filter_maker.source_obj.type == 'CURVE')

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        source = ob.filter_maker.source_obj
        if source is None or source.type != 'CURVE':
            self.report({'ERROR'}, "This filter is not linked to a curve source.")
            return {'CANCELLED'}
        try:
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            source.hide_set(False)
            for item in context.view_layer.objects:
                if item is not None:
                    item.select_set(False)
            source.select_set(True)
            context.view_layer.objects.active = source
            bpy.ops.object.mode_set(mode='EDIT')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, "Could not enter curve Edit Mode: %s" % exc)
            return {'CANCELLED'}


class FILTER_OT_apply_curve_edit(Operator):
    """Leave Edit Mode on the source curve and rebuild the filter from it"""
    bl_idname = "filter.apply_curve_edit"
    bl_label = "Apply Curve Edit"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = active_filter(context)
        return bool(ob and ob.filter_maker.source_obj and ob.filter_maker.source_obj.type == 'CURVE')

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        refresh_curve_source(ob)
        regenerate(ob)
        for item in context.view_layer.objects:
            if item is not None:
                item.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        return {'FINISHED'}


class FILTER_OT_regenerate(Operator):
    bl_idname = "filter.regenerate"
    bl_label = "Update"

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            refresh_curve_source(ob)
            regenerate(ob)
        return {'FINISHED'}


class FILTER_OT_use_reference(Operator):
    bl_idname = "filter.use_reference"
    bl_label = "Use Reference"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            if p.source_obj and p.source_obj.type == 'MESH':
                self.report({'WARNING'}, "The reference is the object being cut. Choose a different mesh as the Object cutter.")
            else:
                self.report({'WARNING'}, "The reference is not a mesh object.")
        return {'FINISHED'}


class FILTER_OT_finish(Operator):
    """Bake the source Boolean and leave the generated meshes as regular objects."""
    bl_idname = "filter.finish"
    bl_label = "Finish (Bake)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        p = ob.filter_maker
        source = getattr(p, "source_obj", None)
        mod_name = _boolean_mod_name(ob, p)
        source_mod = source.modifiers.get(mod_name) if source is not None and source.type == 'MESH' else None
        if source_mod is not None:
            try:
                with context.temp_override(
                    object=source,
                    active_object=source,
                    selected_objects=[source],
                    selected_editable_objects=[source],
                ):
                    bpy.ops.object.modifier_apply(modifier=source_mod.name)
            except Exception as exc:
                self.report({'WARNING'}, "Could not bake source Boolean: %s" % exc)
                return {'CANCELLED'}

        legacy = ob.modifiers.get(BOOL_NAME)
        if legacy is not None and legacy.type == 'BOOLEAN':
            try:
                with context.temp_override(object=ob, active_object=ob,
                                           selected_objects=[ob], selected_editable_objects=[ob]):
                    bpy.ops.object.modifier_apply(modifier=legacy.name)
            except Exception as exc:
                self.report({'WARNING'}, "Could not bake legacy filter Boolean: %s" % exc)
                return {'CANCELLED'}

        _remove_child(p, "cutter_obj")
        p.is_filter = False
        redraw_all()
        self.report({'INFO'}, "Filter and source Boolean baked into normal meshes.")
        return {'FINISHED'}


class FILTER_OT_delete(Operator):
    bl_idname = "filter.delete"
    bl_label = "Delete Filter"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            _remove_source_boolean(ob, p)
            for attr in CHILD_ATTRS:
                _remove_child(p, attr)
            cache = bpy.data.meshes.get(ob.get("fm_sheet_cache", "") or "")
            me = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
            if cache is not None and cache.users == 0:
                bpy.data.meshes.remove(cache)
        redraw_all()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def active_filter(context):
    """Return active filter or its generated child, safe during addon reloads."""
    try:
        # `Object.filter_maker` can be absent while registering or tearing down.
        if not hasattr(bpy.types.Object, "filter_maker"):
            return None
        obj = context.active_object
        if obj is None:
            return None
        props = getattr(obj, "filter_maker", None)
        if props is not None and props.is_filter:
            return obj
        parent = obj.parent
        if parent is not None:
            parent_props = getattr(parent, "filter_maker", None)
            if parent_props is not None and parent_props.is_filter:
                return parent
        # When the source curve is selected for editing, keep its linked filter
        # panel alive so the user can adjust and regenerate the result in place.
        for candidate in bpy.data.objects:
            candidate_props = getattr(candidate, "filter_maker", None)
            if (candidate_props is not None and candidate_props.is_filter
                    and candidate_props.source_obj == obj):
                return candidate
    except (AttributeError, ReferenceError, RuntimeError):
        return None
    return None


def _counts(ob):
    p = ob.filter_maker
    v, f, ev, ef = len(ob.data.vertices), len(ob.data.polygons), 0, 0
    for attr in ("border_obj",):
        child = getattr(p, attr)
        if child is not None and child.type == 'MESH':
            ev += len(child.data.vertices)
            ef += len(child.data.polygons)
    return v, f, ev, ef


class FILTER_PT_main(Panel):
    bl_label = "Filter Maker Lite  0.7.1"
    bl_idname = "FILTER_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Filter Maker'

    def draw_header(self, context):
        _header_icon(self.layout, 'main', 'MOD_WIREFRAME')

    @safe_draw
    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("filter.new_line", icon=ic('CURVE_PATH'))
        obj = context.active_object
        if obj and obj.type in {'MESH', 'CURVE'}:
            col.operator("filter.from_selection", text="From Curve / Edges", icon=ic('CURVE_DATA')).source = 'CURVE'
            if obj.type == 'MESH':
                col.operator("filter.from_selection", text="From Faces", icon=ic('FACESEL')).source = 'FACE'
        else:
            col.label(text="Select a curve or mesh as the source", icon=ic('INFO'))
        if obj and obj.mode == 'EDIT':
            col.label(text="Edit Mode: uses your selection", icon=ic('RESTRICT_SELECT_OFF'))
        ob = active_filter(context)
        if ob is None:
            return
        p = ob.filter_maker
        if p.source_obj is not None and p.source_obj.type == 'CURVE' and p.mode == 'CURVE':
            if p.source_obj.mode == 'EDIT':
                layout.operator("filter.apply_curve_edit", icon=ic('CHECKMARK'))
            else:
                row = layout.row(align=True)
                row.label(text="Curve guide: " + p.source_obj.name, icon=ic('CURVE_DATA'))
                row.operator("filter.edit_source_curve", text="Edit", icon=ic('GREASEPENCIL'))
        layout.separator()
        row = layout.row(align=True)
        row.prop(p, "live", text="Live", toggle=True, icon=ic('PLAY'))
        row.operator("filter.regenerate", text="", icon=ic('FILE_REFRESH'))
        v, f, ev, ef = _counts(ob)
        box = layout.box()
        box.label(text="Verts %s   Faces %s" % (format(v, ","), format(f, ",")), icon=ic('MESH_DATA'))
        if ev:
            box.label(text="+ border: %s verts" % format(ev, ","), icon=ic('MOD_SOLIDIFY'))
        if p.last_error:
            layout.label(text="Error: " + p.last_error, icon=ic('ERROR'))


class FILTER_SubPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Filter Maker'
    bl_parent_id = 'FILTER_PT_main'

    @classmethod
    def poll(cls, context):
        return active_filter(context) is not None

    def draw_header(self, context):
        _header_icon(self.layout, getattr(self, "header_art", ""), getattr(self, "header_icon", "INFO"))

    def P(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        ob = active_filter(context)
        return ob.filter_maker if ob is not None else None


class FILTER_PT_pleats(FILTER_SubPanel, Panel):
    header_art = "pleats"
    header_icon = "MOD_WIREFRAME"
    bl_label = "Pleats"
    bl_idname = "FILTER_PT_pleats"

    @safe_draw
    def draw(self, context):
        p, c = self.P(context), self.layout.column(align=True)
        if p is None:
            return
        for prop in ("pleat_density", "pleat_depth", "pleat_phase", "detail"):
            c.prop(p, prop)
        row = c.row(align=True)
        row.prop(p, "use_thickness", text="Thickness", toggle=True, icon=ic('MOD_SOLIDIFY'))
        amount = c.row(align=True)
        amount.enabled = p.use_thickness
        amount.prop(p, "thickness", text="Amount")
        c.separator()
        if p.mode == 'CURVE':
            c.prop(p, "height")
            c.prop(p, "height_dir")
        else:
            c.prop(p, "pleat_angle")
            c.separator()
            box = c.box()
            box.prop(p, "face_smooth", text="Smooth Face Transitions")
            sub = box.column(align=True)
            sub.enabled = p.face_smooth
            sub.prop(p, "face_smooth_factor", slider=True)
            sub.prop(p, "face_smooth_cuts")
            box.label(text="Rounds internal face seams before pleating", icon=ic('INFO'))
        c.separator()
        c.label(text="Topology")
        c.prop(p, "mesh_type", expand=True)


class FILTER_PT_bevel(FILTER_SubPanel, Panel):
    header_art = "bevel"
    header_icon = "MOD_BEVEL"
    bl_label = "Pleat Bevel"
    bl_idname = "FILTER_PT_bevel"

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        icon_id = profile_icon(p)
        if icon_id:
            row = self.layout.row()
            row.alignment = 'CENTER'
            row.template_icon(icon_value=icon_id, scale=7.0)
            self.layout.label(text="white = result   orange = cuts   grey = sharp")
        c = self.layout.column(align=True)
        c.prop(p, "bevel_ridge", slider=True)
        c.prop(p, "bevel_valley", slider=True)
        c.prop(p, "bevel_cuts")


class FILTER_PT_shading(FILTER_SubPanel, Panel):
    header_art = "shading"
    header_icon = "SHADING_RENDERED"
    bl_label = "Shading"
    bl_idname = "FILTER_PT_shading"

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        c = self.layout.column(align=True)
        c.prop(p, "smooth")
        c.prop(p, "auto_smooth")
        row = c.row(align=True)
        row.active = p.auto_smooth
        row.prop(p, "auto_smooth_angle")


class FILTER_PT_border(FILTER_SubPanel, Panel):
    header_art = "border"
    header_icon = "MOD_SOLIDIFY"
    bl_label = "Border"
    bl_idname = "FILTER_PT_border"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        _header_icon(self.layout, 'border', 'MOD_SOLIDIFY')
        ob = active_filter(context)
        if ob:
            self.layout.prop(ob.filter_maker, "use_border", text="")

    @safe_draw
    def draw(self, context):
        p, layout = self.P(context), self.layout
        if p is None:
            return
        layout.active = p.use_border
        icon_id = border_icon(p)
        if icon_id:
            row = layout.row()
            row.alignment = 'CENTER'
            row.template_icon(icon_value=icon_id, scale=7.0)
        layout.label(text="white = pleats   orange = border   grey = bounds")
        c = layout.column(align=True)
        c.prop(p, "border_width", text="Width")
        c.separator()
        c.label(text="Shading")
        c.prop(p, "border_smooth")
        c.prop(p, "border_auto_smooth")
        row = c.row(align=True)
        row.active = p.border_auto_smooth
        row.prop(p, "border_auto_smooth_angle")


class FILTER_PT_boolean(FILTER_SubPanel, Panel):
    header_art = "boolean"
    header_icon = "MOD_BOOLEAN"
    bl_label = "Boolean"
    bl_idname = "FILTER_PT_boolean"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        _header_icon(self.layout, 'boolean', 'MOD_BOOLEAN')
        ob = active_filter(context)
        if ob:
            self.layout.prop(ob.filter_maker, "use_boolean", text="")

    @safe_draw
    def draw(self, context):
        p, layout = self.P(context), self.layout
        if p is None:
            return
        layout.active = p.use_boolean
        layout.label(text="Cutter")
        layout.prop(p, "boolean_source", expand=True)
        c = layout.column(align=True)
        if p.boolean_source == 'OBJECT':
            row = c.row(align=True)
            row.prop(p, "boolean_obj", text="")
            row.operator("filter.use_reference", text="", icon=ic('INFO'))
            if p.boolean_obj is None:
                c.label(text="Pick an object (or use the reference)", icon=ic('ERROR'))
        else:
            c.prop(p, "cutter_margin")
            c.prop(p, "show_cutter")
            c.label(text="Box or envelope cuts the complete pleated form", icon=ic('INFO'))
        c.prop(p, "boolean_op", text="Operation")
        c.prop(p, "boolean_solver", text="Solver")
        source = p.source_obj
        if p.use_boolean and (source is None or source.type != 'MESH'):
            layout.label(text="A mesh source is required to cut the source shape", icon=ic('ERROR'))
        else:
            layout.label(text="Boolean cuts the source mesh with the selected cutter", icon=ic('INFO'))
        layout.label(text="Baked on Finish. Border is not cut.", icon=ic('INFO'))


class FILTER_PT_materials(FILTER_SubPanel, Panel):
    header_art = "materials"
    header_icon = "MATERIAL"
    bl_label = "Materials"
    bl_idname = "FILTER_PT_materials"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        layout = self.layout
        col = layout.column(align=True)
        col.template_ID(p, "filter_material", new="material.new")
        if p.use_border:
            col.template_ID(p, "border_material", new="material.new")
        col.separator()
        col.prop(p, "uv_mode")
        sub = col.row(align=True)
        sub.active = p.uv_mode == 'WORLD'
        sub.prop(p, "uv_tile")


class FILTER_PT_actions(FILTER_SubPanel, Panel):
    header_art = "finish"
    header_icon = "CHECKMARK"
    bl_label = "Finish"
    bl_idname = "FILTER_PT_actions"

    @safe_draw
    def draw(self, context):
        row = self.layout.row(align=True)
        row.operator("filter.finish", icon=ic('CHECKMARK'))
        row.operator("filter.delete", text="", icon=ic('TRASH'))


# ---------------------------------------------------------------------------
# Panel artwork: anti-aliased vector line drawings on a transparent background
# ---------------------------------------------------------------------------

ART_WHITE = (0.93, 0.95, 1.0)
ART_ORANGE = (1.0, 0.55, 0.12)
ART_GREY = (0.52, 0.53, 0.60)


class _Art:
    """Tiny RGBA rasteriser (y up, straight alpha). Strokes use distance-based anti-aliasing."""

    def __init__(self, w, h):
        self.W, self.H = w, h
        self.buf = [0.0] * (w * h * 4)

    def _blend(self, x, y, col, a):
        if a <= 0.0 or not (0 <= x < self.W and 0 <= y < self.H):
            return
        k = (y * self.W + x) * 4
        b = self.buf
        da = b[k + 3]
        oa = a + da * (1.0 - a)
        if oa <= 1e-9:
            return
        for i in range(3):
            b[k + i] = (col[i] * a + b[k + i] * da * (1.0 - a)) / oa
        b[k + 3] = oa

    def stroke(self, pts, width, col, closed=False, alpha=1.0):
        """Polyline with round joins. Coverage is merged per stroke so joints do not darken."""
        if len(pts) < 2:
            if pts:
                self.dot(pts[0][0], pts[0][1], width * 0.5, col, alpha)
            return
        segs = list(zip(pts, pts[1:]))
        if closed:
            segs.append((pts[-1], pts[0]))
        r = width * 0.5
        cov = {}
        for (x0, y0), (x1, y1) in segs:
            dx, dy = x1 - x0, y1 - y0
            ll = dx * dx + dy * dy
            xa = max(0, int(math.floor(min(x0, x1) - r - 1)))
            xb = min(self.W - 1, int(math.ceil(max(x0, x1) + r + 1)))
            ya = max(0, int(math.floor(min(y0, y1) - r - 1)))
            yb = min(self.H - 1, int(math.ceil(max(y0, y1) + r + 1)))
            for py in range(ya, yb + 1):
                cy = py + 0.5
                for px in range(xa, xb + 1):
                    cx = px + 0.5
                    if ll > 1e-12:
                        t = ((cx - x0) * dx + (cy - y0) * dy) / ll
                        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    else:
                        t = 0.0
                    ex, ey = x0 + dx * t - cx, y0 + dy * t - cy
                    c = r + 0.5 - math.sqrt(ex * ex + ey * ey)
                    if c > 0.0:
                        c = 1.0 if c > 1.0 else c
                        key = (px, py)
                        if c > cov.get(key, 0.0):
                            cov[key] = c
        for (px, py), c in cov.items():
            self._blend(px, py, col, c * alpha)

    def dashed(self, p0, p1, width, col, on=5.0, off=4.0, alpha=1.0):
        (x0, y0), (x1, y1) = p0, p1
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            return
        pos = 0.0
        while pos < length:
            a, b = pos / length, min(pos + on, length) / length
            self.stroke([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                         (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)], width, col, alpha=alpha)
            pos += on + off

    def dot(self, x, y, r, col, alpha=1.0):
        xa, xb = max(0, int(x - r - 1)), min(self.W - 1, int(x + r + 1))
        ya, yb = max(0, int(y - r - 1)), min(self.H - 1, int(y + r + 1))
        for py in range(ya, yb + 1):
            for px in range(xa, xb + 1):
                c = r + 0.5 - math.hypot(px + 0.5 - x, py + 0.5 - y)
                if c > 0.0:
                    self._blend(px, py, col, min(1.0, c) * alpha)

    def fill(self, poly, col, alpha=1.0):
        """Even-odd scanline fill with 4x vertical supersampling for soft edges."""
        if len(poly) < 3:
            return
        ys = [q[1] for q in poly]
        y0 = max(0, int(math.floor(min(ys))))
        y1 = min(self.H - 1, int(math.ceil(max(ys))))
        n = len(poly)
        for py in range(y0, y1 + 1):
            row = {}
            for sub in range(4):
                yc = py + (sub + 0.5) / 4.0
                xs = []
                for i in range(n):
                    (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
                    if (ya <= yc < yb) or (yb <= yc < ya):
                        xs.append(xa + (yc - ya) * (xb - xa) / (yb - ya))
                xs.sort()
                for i in range(0, len(xs) - 1, 2):
                    l, rr = xs[i], xs[i + 1]
                    for px in range(max(0, int(math.floor(l))), min(self.W - 1, int(math.ceil(rr))) + 1):
                        c = max(0.0, min(px + 1.0, rr) - max(float(px), l))
                        if c > 0:
                            row[px] = row.get(px, 0.0) + c * 0.25
            for px, c in row.items():
                self._blend(px, py, col, min(1.0, c) * alpha)

    def downsampled(self, f):
        """Box-filtered copy at 1/f resolution (premultiplied average)."""
        W, H = self.W // f, self.H // f
        out = [0.0] * (W * H * 4)
        b = self.buf
        inv = 1.0 / (f * f)
        for y in range(H):
            for x in range(W):
                r = g = bl = a = 0.0
                for yy in range(y * f, y * f + f):
                    for xx in range(x * f, x * f + f):
                        k = (yy * self.W + xx) * 4
                        aa = b[k + 3]
                        r += b[k] * aa
                        g += b[k + 1] * aa
                        bl += b[k + 2] * aa
                        a += aa
                k = (y * W + x) * 4
                if a > 1e-9:
                    out[k], out[k + 1], out[k + 2] = r / a, g / a, bl / a
                out[k + 3] = a * inv
        return W, H, out


# ---- header icons --------------------------------------------------------
# Each icon is drawn on a 0..1 square (y up) and rasterised at 32 x 32.

def _ic_zig(n=3, x0=0.12, x1=0.88, ylo=0.32, yhi=0.68):
    pts = []
    for i in range(2 * n + 1):
        pts.append((x0 + (x1 - x0) * i / (2 * n), yhi if i % 2 else ylo))
    return pts


def _ic_circle(cx, cy, r, a0=0.0, a1=2 * math.pi, n=40):
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


def _ic_round_zig(cuts=5):
    tab = _profile_table(0.17, 0.17, cuts)
    ev = _make_eval(tab)
    pts = []
    for i in range(61):
        x = i / 60.0
        pts.append((0.1 + 0.8 * x, 0.5 + 0.2 * ev(x * 2.0 - 0.25)))
    return pts


def _icon_shapes(name):
    """List of ('stroke'|'dot'|'dash', data...) in unit space."""
    S = []
    if name == 'main':
        S.append(('stroke', [(0.12, 0.18), (0.88, 0.18), (0.88, 0.82), (0.12, 0.82)], True))
        S.append(('stroke', _ic_zig(3, 0.24, 0.76, 0.32, 0.68), False))
    elif name == 'pleats':
        S.append(('stroke', _ic_zig(3), False))
        S.append(('stroke', [(0.12, 0.2), (0.88, 0.2)], False, 0.45))
    elif name == 'bevel':
        S.append(('stroke', _ic_round_zig(), False))
        S.append(('dash', (0.5, 0.86), (0.5, 0.14)))
    elif name == 'shading':
        S.append(('stroke', _ic_circle(0.5, 0.5, 0.34), True))
        S.append(('stroke', [(0.5 + 0.12 * math.cos(t), 0.5 + 0.34 * math.sin(t))
                             for t in [math.pi * (-0.5 + i / 20.0) for i in range(21)]], False))
        for k in range(3):                      # hatching on the shadow side
            y = 0.34 + k * 0.16
            S.append(('stroke', [(0.66, y - 0.05), (0.78, y + 0.05)], False, 0.75))
    elif name == 'border':
        S.append(('stroke', [(0.1, 0.18), (0.24, 0.18), (0.24, 0.82), (0.1, 0.82)], True))
        S.append(('stroke', [(0.76, 0.18), (0.9, 0.18), (0.9, 0.82), (0.76, 0.82)], True))
        S.append(('stroke', _ic_zig(2, 0.24, 0.76, 0.34, 0.66), False))
    elif name == 'boolean':
        S.append(('stroke', [(0.12, 0.36), (0.6, 0.36), (0.6, 0.86), (0.12, 0.86)], True))
        S.append(('stroke', _ic_circle(0.62, 0.38, 0.25), True))
    elif name == 'materials':
        S.append(('stroke', _ic_circle(0.5, 0.56, 0.3), True))
        S.append(('stroke', [(0.5 + 0.3 * math.cos(a), 0.56 + 0.09 * math.sin(a))
                             for a in [math.pi * (1 + i / 16.0) for i in range(17)]], False, 0.8))
        S.append(('dot', (0.4, 0.68, 0.045)))
        S.append(('stroke', [(0.26, 0.12), (0.74, 0.12)], False))
        S.append(('stroke', [(0.5, 0.26), (0.5, 0.12)], False))
    elif name == 'finish':
        S.append(('stroke', _ic_circle(0.5, 0.5, 0.36), True))
        S.append(('stroke', [(0.32, 0.5), (0.45, 0.36), (0.7, 0.64)], False))
    return S


ICON_NAMES = ('main', 'pleats', 'bevel', 'shading', 'border', 'boolean', 'materials', 'finish')


def _render_icon(name, size=32, ss=2):
    """Returns (w, h, pixels) for one header icon, drawn at ss x size and box-filtered down."""
    N = size * ss
    art = _Art(N, N)
    lw = 2.1 * ss
    for item in _icon_shapes(name):
        kind = item[0]
        if kind == 'stroke':
            alpha = item[3] if len(item) > 3 else 1.0
            art.stroke([(x * N, y * N) for x, y in item[1]], lw, ART_WHITE, closed=item[2], alpha=alpha)
        elif kind == 'dash':
            art.dashed((item[1][0] * N, item[1][1] * N), (item[2][0] * N, item[2][1] * N),
                       lw * 0.6, ART_WHITE, on=3.0 * ss, off=2.5 * ss, alpha=0.6)
        elif kind == 'dot':
            x, y, r = item[1]
            art.dot(x * N, y * N, r * N, ART_WHITE)
        elif kind == 'ring':
            x, y, r = item[1]
            art.stroke([(x * N + r * N * math.cos(t), y * N + r * N * math.sin(t))
                        for t in [2 * math.pi * i / 24 for i in range(25)]], lw, ART_WHITE)
    return art.downsampled(ss)


# ---- the two diagram graphics -----------------------------------------------

def _paint_profile_art(rp, rv, cuts, N=160):
    """Two pleats: grey = sharp fold, white = bevelled result, orange = mesh cuts."""
    art = _Art(N, N)
    table = _profile_table(rp, rv, cuts)
    ev = _make_eval(table)
    m, cy, A = 12, N * 0.5, N * 0.30

    def to_px(X, w):
        return m + (X + 0.5) / 2.0 * (N - 2 * m), cy + w * A

    art.dashed((m, cy), (N - m, cy), 1.0, ART_GREY, on=3, off=3, alpha=0.8)
    ghost = [(-0.5, -1.0), (0.0, 1.0), (0.5, -1.0), (1.0, 1.0), (1.5, -1.0)]
    art.stroke([to_px(X, w) for X, w in ghost], 1.4, ART_GREY)
    seq = sorted({(k + x, w) for k in range(-1, 3) for x, w in table if -0.5 <= k + x <= 1.5})
    seq = [(-0.5, ev(-0.5))] + [s for s in seq if s[0] > -0.5 + 1e-9] + [(1.5, ev(1.5))]
    art.stroke([to_px(X, w) for X, w in seq], 2.6, ART_WHITE)
    for X, w in seq[1:-1]:
        x, y = to_px(X, w)
        art.dot(x, y, 2.6, ART_ORANGE)
    return art


def _paint_border_art(p, N=160):
    """Cross-section across the whole filter, after the sketch:
    white = pleats, orange = border frame, grey = bounding lines."""
    art = _Art(N, N)
    W, O, F, B, fit = _border_dims(p)
    depth = max(p.pleat_depth, 1e-6)
    top, bot = max(F, depth), -max(B, depth)
    my = 30.0
    ky = (N - 2 * my) / max(top - bot, 1e-9)
    side = (O + W) * ky
    max_side = N * 0.21
    kx = ky if side <= max_side else ky * max_side / max(side, 1e-9)
    mx = 14.0
    edge_l = mx + O * kx
    edge_r = N - mx - O * kx

    def Y(h):
        return my + (h - bot) * ky

    # Grey bounding lines (drawn first, like construction lines that overshoot).
    yb = Y(bot)
    art.stroke([(4, yb), (N - 4, yb)], 1.6, ART_GREY)
    for xe in (edge_l, edge_r):
        art.stroke([(xe, max(3.0, Y(bot) - 16)), (xe, min(N - 3.0, Y(top) + 16))], 1.6, ART_GREY)

    # White pleats with the real bevel profile, spanning edge to edge.
    table = _profile_table(*_bevel_radii(p), p.bevel_cuts)
    ev = _make_eval(table)
    span = max(edge_r - edge_l, 1.0)
    period_px = ky / max(p.pleat_density, 1e-6)
    period_px = min(max(period_px, span / 6.0), span / 2.5)
    xs = set(i * 1.5 for i in range(int(span / 1.5) + 1))
    xs.add(span)
    for k in range(-1, int(span / period_px) + 2):
        for tx, _w in table:
            x = (k + tx - p.pleat_phase) * period_px
            if 0.0 <= x <= span:
                xs.add(x)
    pts = [(edge_l + x, Y(depth * ev(x / period_px + p.pleat_phase))) for x in sorted(xs)]
    art.stroke(pts, 2.4, ART_WHITE)

    # Orange border frames (actual cross-section), mirrored on both edges.
    sec = _border_section(p, W, O, F, B, fit)
    left = [(edge_l + s * kx, Y(h)) for s, h in sec]
    right = [(edge_r - s * kx, Y(h)) for s, h in sec]
    for poly in (left, right):
        art.fill(poly, ART_ORANGE, alpha=0.16)
        art.stroke(poly, 2.2, ART_ORANGE, closed=True)
    return art


def _set_preview(pv, art, icon_px=32):
    """Fill both the large image and the small icon buffers of an ImagePreview."""
    pv.image_size = (art.W, art.H)
    pv.image_pixels_float = art.buf
    f = max(1, art.W // icon_px)
    w, h, small = art.downsampled(f)
    pv.icon_size = (w, h)
    pv.icon_pixels_float = small


def profile_icon(p):
    pv = _pv.get("profile")
    if pv is None:
        return 0
    rp, rv = _bevel_radii(p)
    key = (round(rp, 3), round(rv, 3), int(p.bevel_cuts))
    if _pv_key.get("profile") != key:
        try:
            _set_preview(pv, _paint_profile_art(rp, rv, p.bevel_cuts))
            _pv_key["profile"] = key
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id


def border_icon(p):
    pv = _pv.get("border")
    if pv is None:
        return 0
    W, O, F, B, fit = _border_dims(p)
    key = (round(W, 6), round(O, 6), round(F, 6), round(B, 6), round(fit, 6), round(p.pleat_depth, 6),
           round(p.pleat_density, 3), round(p.pleat_phase, 3), round(p.bevel_ridge, 3),
           round(p.bevel_valley, 3), int(p.bevel_cuts))
    if _pv_key.get("border") != key:
        try:
            _set_preview(pv, _paint_border_art(p))
            _pv_key["border"] = key
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id


def art_icon(name):
    """icon_id of a custom header icon (painted once, lazily), or 0."""
    key = "icon_" + name
    pv = _pv.get(key)
    if pv is None and _pcoll[0] is not None and name in ICON_NAMES:
        try:
            pv = _pcoll[0].new(key)
            w, h, px = _render_icon(name)
            pv.icon_size = (w, h)
            pv.icon_pixels_float = px
            pv.image_size = (w, h)
            pv.image_pixels_float = px
            _pv[key] = pv
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id if pv is not None else 0


def _header_icon(layout, name, fallback):
    icon_id = art_icon(name) if name else 0
    if icon_id:
        layout.label(text="", icon_value=icon_id)
    else:
        layout.label(text="", icon=ic(fallback))


classes = (FilterProps, FILTER_OT_new_line, FILTER_OT_from_selection, FILTER_OT_edit_source_curve,
           FILTER_OT_apply_curve_edit, FILTER_OT_regenerate, FILTER_OT_use_reference, FILTER_OT_finish,
           FILTER_OT_delete, FILTER_PT_main, FILTER_PT_pleats, FILTER_PT_bevel, FILTER_PT_shading,
           FILTER_PT_border, FILTER_PT_boolean, FILTER_PT_materials, FILTER_PT_actions)


# ---------------------------------------------------------------------------
# Robust registration / reload cleanup
# ---------------------------------------------------------------------------

def _teardown():
    """Unregister classes, properties and previews."""
    if _pcoll[0] is not None:
        try:
            bpy.utils.previews.remove(_pcoll[0])
        except Exception:
            pass
    _pcoll[0] = None
    _pv.clear()
    _pv_key.clear()

    # Resolve the registered RNA type by name: classes from previous script
    # executions are different Python objects and cannot be unregistered by the
    # current module's class references alone.
    for cls in reversed(classes):
        try:
            registered = getattr(bpy.types, cls.__name__, None)
            if registered is not None:
                bpy.utils.unregister_class(registered)
            else:
                bpy.utils.unregister_class(cls)
        except Exception:
            pass

    for owner, attr in ((bpy.types.Object, "filter_maker"),):
        try:
            if hasattr(owner, attr):
                delattr(owner, attr)
        except Exception:
            pass


def register():
    _teardown()
    try:
        # Register the property group and pointer before any panel can poll/draw.
        bpy.utils.register_class(FilterProps)
        bpy.types.Object.filter_maker = PointerProperty(type=FilterProps)
        for cls in classes:
            if cls is not FilterProps:
                bpy.utils.register_class(cls)
        _pcoll[0] = bpy.utils.previews.new()
        _pv["profile"] = _pcoll[0].new("profile")
        _pv["border"] = _pcoll[0].new("border")
        _pv_key.clear()
        for name in ICON_NAMES:
            art_icon(name)
    except Exception:
        traceback.print_exc()
        _teardown()
        raise


def unregister():
    _teardown()


if __name__ == "__main__":
    register()
