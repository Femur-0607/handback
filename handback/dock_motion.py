"""Pure motion math for the dashboard's tactile drag and taskbar docking.

Everything here works in physical pixels and takes the window DPI, so the
feel is the same at 100% and 200% scaling. No Tk or Win32 calls.
"""
import math

# Logical (96-DPI) pixels.
MAGNET_RADIUS = 56
BREAKAWAY_DISTANCE = 28
BREAKAWAY_STRETCH = 0.45
SETTLE_MS = 200
SETTLE_FRAME_MS = 16
SETTLE_DAMPING = 0.72
# How far toward the dock an approach leans before the drop would actually dock.
MAGNET_LEAN = 0.6
# How far a drop that would dock is drawn in while still held; release springs the rest.
DOCK_PREVIEW = 0.8


def scaled(logical, dpi):
    return max(1, round(logical * dpi / 96))


def ease_in_out(t):
    """Smoothstep-like cubic: no jolt as the pull starts or completes."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def taskbar_gap(info, cursor):
    """Distance from the pointer to a horizontal taskbar on its desktop side.

    0 inside the taskbar, None when the taskbar cannot dock or the pointer is
    beside it horizontally (the magnet only works across the facing edge).
    """
    if not info or info.get("edge") not in (1, 3) or info.get("auto_hide"):
        return None
    left, top, right, bottom = info["rect"]
    if right <= left or bottom <= top:
        return None
    x, y = cursor
    if not left <= x < right:
        return None
    if top <= y < bottom:
        return 0
    if info["edge"] == 3:
        return top - y if y < top else None
    return y - bottom + 1 if y >= bottom else None


def magnet_strength(gap, radius):
    """0 outside the radius, rising smoothly to 1 at the taskbar edge."""
    if gap is None or gap >= radius:
        return 0.0
    if gap <= 0:
        return 1.0
    return ease_in_out(1 - gap / radius)


def magnet_pull(position, target, strength):
    """Blend the raw drag position toward the dock target; rounds to pixels."""
    if strength <= 0:
        return tuple(position)
    if strength >= 1:
        return tuple(target)
    return tuple(round(p + (t - p) * strength) for p, t in zip(position, target))


def breakaway(offset, distance, stretch=BREAKAWAY_STRETCH):
    """Rubber-band a docked widget until the pull exceeds ``distance``.

    Returns (shown_offset, broke). Below the threshold the widget lags behind
    the pointer and the lag grows with the pull; at the threshold it pops free
    and follows the pointer 1:1 again.
    """
    dx, dy = offset
    length = math.hypot(dx, dy)
    if length >= distance:
        return (dx, dy), True
    if length == 0:
        return (0, 0), False
    # tanh keeps the stretch monotonic and saturating, so it feels like tension.
    shown = distance * stretch * math.tanh(length / distance)
    scale = shown / length
    return (round(dx * scale), round(dy * scale)), False


def spring_offset(t, omega, damping):
    """Remaining fraction (1 at t=0, 0 at rest) of a unit-mass spring released at rest."""
    if damping >= 1:
        return (1 + omega * t) * math.exp(-omega * t)
    damped = omega * math.sqrt(1 - damping * damping)
    decay = math.exp(-damping * omega * t)
    return decay * (math.cos(damped * t) + damping * omega / damped * math.sin(damped * t))


def settle_frames(start, target, duration_ms=SETTLE_MS, frame_ms=SETTLE_FRAME_MS,
                  damping=SETTLE_DAMPING):
    """Positions for a short spring settle; the last frame is exactly ``target``.

    The stiffness is chosen so the motion has visibly settled (under 2% of the
    distance) by ``duration_ms``; damping < 1 adds a small overshoot.
    """
    start, target = tuple(start), tuple(target)
    if start == target or duration_ms <= 0:
        return [target]
    # The envelope exp(-zeta*omega*t) reaches 2% at the end of the duration.
    omega = math.log(50) / (min(damping, 1) * duration_ms / 1000)
    count = max(1, math.ceil(duration_ms / frame_ms))
    # Keep one frame per tick (even repeats) so the rhythm stays even, but stop
    # as soon as the rest of the motion rounds to the target.
    frames = [tuple(round(t + (s - t) * spring_offset(index * frame_ms / 1000, omega, damping))
                    for s, t in zip(start, target))
              for index in range(1, count)]
    while frames and frames[-1] == target:
        frames.pop()
    return frames + [target]
