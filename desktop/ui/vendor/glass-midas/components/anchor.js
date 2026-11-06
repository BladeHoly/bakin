/**
 * Places a fixed element next to `target`: on the side it asks for (`above`), flipped to the
 * other when that side has no room, centered on the target but kept inside the viewport. Marks
 * the side it landed on as `data-side` and returns the target's center from the element's left
 * edge, where an arrow goes.
 */
export function anchorTo(el, target, above, gap = 12) {
    const margin = 8;
    const a = target.getBoundingClientRect();
    const width = el.offsetWidth;
    const height = el.offsetHeight;
    const center = a.left + a.width / 2;
    // A box wider than the viewport stays on the left margin rather than off it.
    const left = Math.max(Math.min(center - width / 2, innerWidth - width - margin), margin);
    const fitsAbove = a.top - height - gap >= margin;
    const fitsBelow = a.bottom + gap + height <= innerHeight - margin;
    const up = above ? fitsAbove || !fitsBelow : !fitsBelow && fitsAbove;
    // Fixed is how an anchored element works, not a style: inline, so no page rule can undo it.
    el.style.position = 'fixed';
    el.style.left = `${left}px`;
    el.style.top = `${up ? a.top - height - gap : a.bottom + gap}px`;
    el.dataset.side = up ? 'top' : 'bottom';
    return center - left;
}
/** Calls `place` on the next frame after any scroll or resize; returns the function that stops. */
export function follow(place) {
    let frame = 0;
    const schedule = () => {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(place);
    };
    addEventListener('scroll', schedule, { passive: true, capture: true });
    addEventListener('resize', schedule);
    schedule();
    return () => {
        cancelAnimationFrame(frame);
        removeEventListener('scroll', schedule, { capture: true });
        removeEventListener('resize', schedule);
    };
}
//# sourceMappingURL=anchor.js.map