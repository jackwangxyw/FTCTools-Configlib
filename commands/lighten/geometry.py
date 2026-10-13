"""Pocket geometry for the Lighten feature.

All work is done on planar sheet bodies with the TemporaryBRepManager, so nothing
here touches the timeline. The pocket region is

    grow(shrink(face, wall + r) - bands(special edges, 2 (special wall + r)) - bands(struts, strut + 2r)
         - grow(exclusions, wall + r), r)

grow and shrink are exact offsets with round corners, built as

    grow(R, d)   = R + (boundary of R swept by a disk of radius d)
    shrink(R, d) = R - (boundary of R swept by a disk of radius d)

The swept boundary is the union of a band around every edge plus a disk at every
vertex. That only needs explicit lines, arcs and circles plus booleans, so it
does not care about curve direction (mirrored input gives mirrored output) and
it survives regions that pinch to a point.

The final grow by r is what rounds every inside corner of the remaining
material (strut/strut and strut/wall junctions) to the tool radius.
"""

import adsk.core
import adsk.fusion

DIFFERENCE = adsk.fusion.BooleanTypes.DifferenceBooleanType
UNION = adsk.fusion.BooleanTypes.UnionBooleanType
INTERSECTION = adsk.fusion.BooleanTypes.IntersectionBooleanType
EPS = 1e-7  # cm
SLIVER = 1e-5  # cm: a region thinner than this is boolean debris, not a pocket
CHORD = 2e-3   # cm: how far the straight pieces standing in for a spline may stray from it


class LightenError(Exception):
    """An input problem the user can fix. message_id is the Fusion status message to show on the feature."""

    def __init__(self, message, message_id='API_COMPUTE_ERROR'):
        super().__init__(message)
        self.message_id = message_id


def _tb():
    return adsk.fusion.TemporaryBRepManager.get()


def _face(curves):
    """Planar face bounded by one closed chain of curves."""
    tb = _tb()
    wire, _ = tb.createWireFromCurves(curves, False)
    if wire is None:
        raise LightenError('Could not build a closed loop.')
    face = tb.createFaceFromPlanarWires([wire])
    if face is None:
        raise LightenError('Could not build a face from a closed loop.')
    return face


def _merge(target, tool, op):
    """Boolean tool into target. None means an empty region."""
    if target is None:
        return None if op == DIFFERENCE else tool
    if tool is None:
        return target
    if not _tb().booleanOperation(target, tool, op):
        raise LightenError('Boolean operation failed.')
    return target if target.faces.count else None


def _disk(center, normal, radius):
    return _face([adsk.core.Circle3D.createByCenter(center, normal, radius)])


def _scaled(center, point, radius):
    """The point at the given distance from center, in the direction of point."""
    v = center.vectorTo(point)
    v.normalize()
    v.scaleBy(radius)
    p = center.copy()
    p.translateBy(v)
    return p


def _offset_point(p, direction, dist):
    v = direction.copy()
    v.scaleBy(dist)
    q = p.copy()
    q.translateBy(v)
    return q


def _arc_points(arc):
    ev = arc.evaluator
    _, t0, t1 = ev.getParameterExtents()
    _, pts = ev.getPointsAtParameters([t0, (t0 + t1) / 2, t1])
    return pts


def band(curve, normal, half):
    """Region within `half` of the curve, measured perpendicular to it (flat ends).

    Lines give a rectangle, arcs an annular sector (a pie slice when the arc is
    tighter than `half`), circles an annulus (a disk when tighter than `half`).
    Splines and ellipses (text outlines, for one) have no exact offset: they
    are followed with straight pieces within CHORD, a rectangle for each and
    a disk at each joint between them.
    """
    kind = curve.objectType
    if kind == adsk.core.Line3D.classType():
        p0, p1 = curve.startPoint, curve.endPoint
        side = normal.crossProduct(p0.vectorTo(p1))
        if side.length < EPS:
            return None
        side.normalize()
        corners = [_offset_point(p0, side, half), _offset_point(p1, side, half),
                   _offset_point(p1, side, -half), _offset_point(p0, side, -half)]
        return _face([adsk.core.Line3D.create(corners[i], corners[(i + 1) % 4]) for i in range(4)])

    if kind == adsk.core.Circle3D.classType():
        outer = _disk(curve.center, normal, curve.radius + half)
        if curve.radius - half <= EPS:
            return outer
        return _merge(outer, _disk(curve.center, normal, curve.radius - half), DIFFERENCE)

    if kind == adsk.core.Arc3D.classType():
        c = curve.center
        r_out = curve.radius + half
        r_in = curve.radius - half
        start, mid, end = _arc_points(curve)
        outer = adsk.core.Arc3D.createByThreePoints(*(_scaled(c, p, r_out) for p in (start, mid, end)))
        if r_in <= EPS:
            edges = [outer,
                     adsk.core.Line3D.create(_scaled(c, end, r_out), c),
                     adsk.core.Line3D.create(c, _scaled(c, start, r_out))]
        else:
            inner = adsk.core.Arc3D.createByThreePoints(*(_scaled(c, p, r_in) for p in (start, mid, end)))
            edges = [outer, inner,
                     adsk.core.Line3D.create(_scaled(c, start, r_in), _scaled(c, start, r_out)),
                     adsk.core.Line3D.create(_scaled(c, end, r_in), _scaled(c, end, r_out))]
        return _face(edges)

    ev = curve.evaluator
    _, t0, t1 = ev.getParameterExtents()
    ok, points = ev.getStrokes(t0, t1, CHORD)
    if not ok or len(points) < 2:
        raise LightenError('Could not follow a %s edge.' % kind.split('::')[-1])
    pieces = [band(adsk.core.Line3D.create(points[i], points[i + 1]), normal, half) for i in range(len(points) - 1)]
    pieces += [_disk(p, normal, half) for p in points[1:-1]]
    return _union_all([p for p in pieces if p is not None])


def swept_boundary(region, normal, dist):
    """Every point within dist of the region's boundary."""
    def pieces():
        made = [band(edge.geometry, normal, dist) for edge in region.edges]
        made += [_disk(vertex.geometry, normal, dist) for vertex in region.vertices]
        return [p for p in made if p is not None]

    try:
        return _union_all(pieces())
    except RuntimeError:
        # The pieces share edges and tangencies wherever holes and slots sit
        # close together, and the kernel can reject one union order of them
        # (ASM_INCONS_VERT_ATT) and accept another. One at a time is slower
        # but joins the same pieces. The first try changed them, so remake.
        made = pieces()
        for piece in made[1:]:
            _merge(made[0], piece, UNION)
        return made[0]


def _union_all(bodies):
    """Union in a balanced tree. Folding into one growing body costs far more on big plates."""
    while len(bodies) > 1:
        paired = []
        for i in range(0, len(bodies) - 1, 2):
            paired.append(_merge(bodies[i], bodies[i + 1], UNION))
        if len(bodies) % 2:
            paired.append(bodies[-1])
        bodies = paired
    return bodies[0] if bodies else None


def _without_slivers(region):
    """The region without faces thinner than SLIVER, or None if none are left.
    Offsetting a face with close holes and slots can leave triangles a few
    hundredths of a micron wide where two offsets nearly touch; the kernel
    can't offset those again (ASM_BODY_VERTEX_CRUMBLE)."""
    slivers = [f for f in region.faces if f.area < SLIVER * sum(e.length for e in f.edges)]
    if not slivers:
        return region
    if len(slivers) == region.faces.count:
        return None
    if not _tb().deleteFaces(slivers, True):
        raise LightenError('Could not clean up the pocket region.')
    return region


def grow(region, normal, dist):
    return _merge(region, swept_boundary(region, normal, dist), UNION)


def shrink(region, normal, dist):
    return _merge(region, swept_boundary(region, normal, dist), DIFFERENCE)


def _translated(geom, vec):
    g = geom.copy()
    m = adsk.core.Matrix3D.create()
    m.translation = vec
    g.transformBy(m)
    return g


def _point_plus(p, vec):
    q = p.copy()
    q.translateBy(vec)
    return q


def loop_coedges(loop):
    """(edge, isOpposedToEdge) for each coedge of a loop, in loop order.

    Walked by coedge.next: the API's B-rep collections cost O(n) per item, so
    `for e in face.edges` (or vertices, coEdges) is quadratic on a face with
    thousands of edges, like a long belt or a big pulley."""
    first = loop.coEdges.item(0)
    out = []
    ce = first
    while True:
        out.append((ce.edge, ce.isOpposedToEdge))
        ce = ce.next
        if ce == first:
            return out


def body_edges(body):
    """Every edge of a body once, found through its loops (see loop_coedges)."""
    seen = set()
    for face in body.faces:
        for loop in face.loops:
            for edge, _ in loop_coedges(loop):
                if edge.tempId not in seen:
                    seen.add(edge.tempId)
                    yield edge


def prism(sheet, vec):
    """Solid made by sweeping every face of a planar sheet body along vec.

    The TemporaryBRepManager has no extrude, so the solid is defined face by
    face: a cap on each end plus one side face per edge (a plane for a line, a
    cylinder for an arc or circle). Coedge directions follow the rule that each
    face is traversed counterclockwise about its outward normal, which makes
    every edge appear once in each direction.
    """
    d = vec.copy()
    d.normalize()
    up = d.copy()
    up.scaleBy(-1)  # outward normal of the starting cap

    body_def = adsk.fusion.BRepBodyDefinition.create()
    for face in sheet.faces:
        shell = body_def.lumpDefinitions.add().shellDefinitions.add()
        face_loops = [loop_coedges(loop) for loop in face.loops]
        top_v = {}
        bot_v = {}
        vertical = {}
        top_e = {}
        bot_e = {}
        for coedges in face_loops:
            for e, _ in coedges:
                for v in (e.startVertex, e.endVertex):
                    if v.tempId in top_v:
                        continue
                    p = v.geometry
                    top_v[v.tempId] = body_def.createVertexDefinition(p)
                    bot_v[v.tempId] = body_def.createVertexDefinition(_point_plus(p, vec))
                    vertical[v.tempId] = body_def.createEdgeDefinitionByCurve(
                        top_v[v.tempId], bot_v[v.tempId], adsk.core.Line3D.create(p, _point_plus(p, vec)))
            for e, _ in coedges:
                s, t = e.startVertex.tempId, e.endVertex.tempId
                top_e[e.tempId] = body_def.createEdgeDefinitionByCurve(top_v[s], top_v[t], e.geometry)
                bot_e[e.tempId] = body_def.createEdgeDefinitionByCurve(bot_v[s], bot_v[t], _translated(e.geometry, vec))

        # Coedges of each loop, ordered counterclockwise about `up`.
        _, n_face = face.evaluator.getNormalAtPoint(face.pointOnFace)
        same = n_face.dotProduct(up) > 0
        loops = []
        for coedges in face_loops:
            if not same:
                coedges = [(e, not opp) for e, opp in reversed(coedges)]
            loops.append(coedges)

        plane = face.geometry
        top = shell.faceDefinitions.add(plane, plane.normal.dotProduct(up) < 0)
        bot = shell.faceDefinitions.add(_translated(plane, vec), plane.normal.dotProduct(d) < 0)
        for coedges in loops:
            top_loop = top.loopDefinitions.add()
            for e, opp in coedges:
                top_loop.bRepCoEdgeDefinitions.add(top_e[e.tempId], opp)
            bot_loop = bot.loopDefinitions.add()
            for e, opp in reversed(coedges):
                bot_loop.bRepCoEdgeDefinitions.add(bot_e[e.tempId], not opp)

            for e, opp in coedges:
                a = e.endVertex if opp else e.startVertex
                b = e.startVertex if opp else e.endVertex
                side = shell.faceDefinitions.add(*_side_surface(e, opp, up))
                side_loop = side.loopDefinitions.add()
                side_loop.bRepCoEdgeDefinitions.add(top_e[e.tempId], not opp)   # b -> a along the top
                side_loop.bRepCoEdgeDefinitions.add(vertical[a.tempId], False)  # down at a
                side_loop.bRepCoEdgeDefinitions.add(bot_e[e.tempId], opp)       # a -> b along the bottom
                side_loop.bRepCoEdgeDefinitions.add(vertical[b.tempId], True)   # up at b

    body = body_def.createBody()
    if body is None or not body.isSolid:
        raise LightenError('Could not build the pocket solid.')
    return body


def _side_surface(edge, opp, up):
    """(surface, isParamReversed) for the side wall under an edge.

    Walking the edge in loop order with `up` outward, the region is on the
    left, so the wall's outward normal is tangent x up.
    """
    ev = edge.evaluator
    _, t0, t1 = ev.getParameterExtents()
    tm = (t0 + t1) / 2
    _, pm = ev.getPointAtParameter(tm)
    _, tangent = ev.getFirstDerivative(tm)
    if opp:
        tangent.scaleBy(-1)
    outward = tangent.crossProduct(up)
    g = edge.geometry
    kind = g.objectType
    if kind == adsk.core.Line3D.classType():
        outward.normalize()
        return adsk.core.Plane.create(pm, outward), False
    if kind in (adsk.core.Arc3D.classType(), adsk.core.Circle3D.classType()):
        cyl = adsk.core.Cylinder.create(g.center, up, g.radius)
        radial = g.center.vectorTo(pm)
        return cyl, radial.dotProduct(outward) < 0
    raise LightenError('Only lines, arcs and circles are supported (got %s).' % kind.split('::')[-1])


def trim_thin(region, normal, half_width):
    """Cut thin fingers and necks off each pocket region, keeping ordinary corners.

    Per face (each face is one pocket before the final grow):
        core  = shrink(face, a)      the parts at least 2a wide
        keep  = grow(core, 2a)       everything within 2a of the core
        face & keep keeps corners of 60 degrees or wider whole (their tip is
        within a / sin(30) = 2a of the core) and cuts off long thin fingers and
        narrow channels. The caller's final grow rounds the cut ends.
    A pocket narrower than 2a everywhere has no core. Rather than erase it or
    leave its fingers, it is trimmed at its own scale: a is halved (up to
    three times) until a core exists. Only a pocket too small for that is
    kept whole.
    """
    out = None
    for face in region.faces:
        piece = _tb().copy(face)
        a = half_width
        for _ in range(4):
            core = shrink(_tb().copy(piece), normal, a)
            if core is not None:
                piece = _merge(piece, grow(core, normal, 2 * a), INTERSECTION)
                break
            a /= 2
        out = _merge(out, piece, UNION)
    return out


def compute_pockets(face, normal, strut_curves, exclusion_loops, wall, strut, radius, min_width=0,
                    special=()):
    """Return a sheet body whose faces are the pockets, lying on the face's plane.

    face            planar BRepFace to lighten
    normal          normal of that face (Vector3D)
    strut_curves    Curve3D objects on the face plane, in the face's component space
    exclusion_loops list of closed loops, each a list of Curve3D on the face plane
    wall, strut, radius, min_width   lengths in cm. min_width 0 (or not above
                    2 * radius) leaves thin pocket fingers alone.
    special         (curves, special wall) pairs: boundary edges of the face
                    (Curve3D) that keep that wall (thicker than wall) instead,
                    along their own length only
    """
    if radius <= 0:
        raise LightenError('Fillet radius must be positive.')

    region = shrink(_tb().copy(face), normal, wall + radius)
    for curves, special_wall in special:
        for curve in curves:
            if region is None:
                return None
            region = _merge(region, band(curve, normal, special_wall + radius), DIFFERENCE)
    for curve in strut_curves:
        if region is None:
            return None
        region = _merge(region, band(curve, normal, strut / 2 + radius), DIFFERENCE)
    for loop in exclusion_loops:
        if region is None:
            return None
        region = _merge(region, grow(_face(loop), normal, wall + radius), DIFFERENCE)
    if region is not None:
        region = _without_slivers(region)
    if region is not None and min_width > 2 * radius:
        # The final grow adds 2 * radius of width, so trim the region at what's left.
        region = trim_thin(region, normal, min_width / 2 - radius)
    if region is None:
        return None
    return grow(region, normal, radius)
