import { Base, define } from './base.js';
const ICON = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const PREVIOUS = 'M15 5l-7 7 7 7';
const NEXT = 'M9 5l7 7-7 7';
/**
 * `<gm-gallery aria-label="Pictures" zoom><img src="…" alt="…">…</gm-gallery>`: pictures one at a
 * time, swiped or scrolled on a snapping track, with a thumbnail for each underneath and arrows
 * either side. Arrow keys, Home and End move through them once the track has focus.
 *
 * With `zoom`, a click (or Enter) opens the picture as large as the screen allows, on a native
 * `<dialog>`; a click there magnifies where the pointer is. A thumbnail uses `data-thumb` when a
 * picture has one, else its `src`, and follows when the `src` changes (a finish switched). Moving
 * to another picture fires `gm-change` with its `index`.
 */
export class GmGallery extends Base {
    #track;
    #thumbs;
    #arrows = [];
    #index = 0;
    #frame = 0;
    /** Where show() is scrolling to; until it gets there, the pictures passed on the way don't count. */
    #going;
    #watch;
    #zoom;
    connectedCallback() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.isConnected && this.#build(), {
                once: true,
            });
        }
        else
            this.#build();
    }
    disconnectedCallback() {
        this.#watch?.disconnect();
        cancelAnimationFrame(this.#frame);
    }
    #build() {
        if (this.#track)
            return;
        if (!this.hasAttribute('role'))
            this.setAttribute('role', 'region');
        const track = document.createElement('div');
        track.className = 'gm-gallery-track';
        track.tabIndex = 0;
        track.append(...this.childNodes);
        const button = (cls, label, d, step) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = `gm-gallery-arrow ${cls}`;
            b.setAttribute('aria-label', label);
            b.innerHTML = ICON(d);
            b.addEventListener('click', () => this.show(this.#index + step));
            return b;
        };
        this.#arrows = [
            button('gm-gallery-previous', this.getAttribute('label-previous') ?? 'Previous picture', PREVIOUS, -1),
            button('gm-gallery-next', this.getAttribute('label-next') ?? 'Next picture', NEXT, 1),
        ];
        const thumbs = document.createElement('div');
        thumbs.className = 'gm-gallery-thumbs';
        thumbs.setAttribute('role', 'group');
        thumbs.setAttribute('aria-label', this.getAttribute('label-thumbs') ?? 'Choose a picture');
        thumbs.addEventListener('click', (e) => {
            const b = e.target.closest('button');
            if (b)
                this.show(Number(b.dataset.index));
        });
        const stage = document.createElement('div');
        stage.className = 'gm-gallery-stage';
        stage.append(track, ...this.#arrows);
        this.append(stage, thumbs);
        this.#track = track;
        this.#thumbs = thumbs;
        track.addEventListener('scroll', () => {
            cancelAnimationFrame(this.#frame);
            this.#frame = requestAnimationFrame(() => {
                const at = Math.round(track.scrollLeft / (track.clientWidth || 1));
                if (this.#going === undefined)
                    this.#select(at);
                else if (at === this.#going)
                    this.#going = undefined;
            });
        });
        track.addEventListener('scrollend', () => (this.#going = undefined));
        // A finger or a wheel takes over from a scroll we started.
        for (const type of ['pointerdown', 'wheel', 'touchstart'])
            track.addEventListener(type, () => (this.#going = undefined), { passive: true });
        track.addEventListener('keydown', (e) => {
            const last = this.slides.length - 1;
            const to = {
                ArrowLeft: this.#index - 1,
                ArrowRight: this.#index + 1,
                Home: 0,
                End: last,
            }[e.key];
            if (to !== undefined) {
                e.preventDefault();
                this.show(to);
            }
            else if ((e.key === 'Enter' || e.key === ' ') && this.hasAttribute('zoom')) {
                e.preventDefault();
                this.#openZoom();
            }
        });
        track.addEventListener('click', (e) => {
            if (this.hasAttribute('zoom') && e.target !== track)
                this.#openZoom();
        });
        // New pictures, or a picture's src changed: redraw the thumbnails.
        this.#watch = new MutationObserver(() => this.#drawThumbs());
        this.#watch.observe(track, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['src', 'data-thumb', 'alt'],
        });
        this.#drawThumbs();
    }
    /** The pictures (or whatever was put inside), in order. */
    get slides() {
        return this.#track ? [...this.#track.children] : [];
    }
    get index() {
        return this.#index;
    }
    set index(i) {
        this.show(i);
    }
    /** Scrolls to picture `i` (clamped to the ones there are). */
    show(i, smooth = true) {
        const track = this.#track;
        const slides = this.slides;
        if (!track || !slides.length)
            return;
        const to = Math.max(0, Math.min(slides.length - 1, i));
        const reduce = matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        const at = Math.round(track.scrollLeft / (track.clientWidth || 1));
        this.#going = at === to ? undefined : to;
        track.scrollTo({
            left: to * track.clientWidth,
            behavior: smooth && !reduce ? 'smooth' : 'instant',
        });
        this.#select(to);
    }
    #select(i) {
        const count = this.slides.length;
        const to = Math.max(0, Math.min(count - 1, i));
        const [previous, next] = this.#arrows;
        if (previous)
            previous.disabled = to <= 0;
        if (next)
            next.disabled = to >= count - 1;
        this.#thumbs?.querySelectorAll('button').forEach((b, k) => {
            if (k === to)
                b.setAttribute('aria-current', 'true');
            else
                b.removeAttribute('aria-current');
        });
        this.slides.forEach((s, k) => s.toggleAttribute('inert', k !== to));
        if (to === this.#index)
            return;
        this.#index = to;
        this.dispatchEvent(new CustomEvent('gm-change', { detail: { index: to }, bubbles: true }));
    }
    #drawThumbs() {
        const thumbs = this.#thumbs;
        if (!thumbs)
            return;
        const slides = this.slides;
        this.toggleAttribute('data-single', slides.length < 2);
        thumbs.replaceChildren(...slides.map((slide, k) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.dataset.index = String(k);
            const img = slide.matches('img') ? slide : slide.querySelector('img');
            b.setAttribute('aria-label', `${img?.getAttribute('alt') || 'Picture'} (${k + 1} of ${slides.length})`);
            const src = slide.dataset.thumb ?? img?.getAttribute('src');
            if (src) {
                const t = document.createElement('img');
                t.src = src;
                t.alt = '';
                t.loading = 'lazy';
                b.append(t);
            }
            return b;
        }));
        this.#select(Math.min(this.#index, slides.length - 1));
    }
    #openZoom() {
        const slide = this.slides[this.#index];
        const img = slide?.matches('img') ? slide : slide?.querySelector('img');
        if (!img)
            return;
        if (!this.#zoom) {
            const zoom = document.createElement('dialog');
            zoom.className = 'gm-gallery-zoom';
            zoom.setAttribute('aria-label', this.getAttribute('label-zoom') ?? 'Picture, large');
            const label = (name, fallback) => this.getAttribute(name) ?? fallback;
            zoom.innerHTML =
                `<img alt=""><button type="button" class="gm-gallery-close" aria-label="${label('label-close', 'Close')}">${ICON('M6 6l12 12M18 6L6 18')}</button>` +
                    `<button type="button" class="gm-gallery-arrow gm-gallery-previous" data-step="-1" aria-label="${label('label-previous', 'Previous picture')}">${ICON(PREVIOUS)}</button>` +
                    `<button type="button" class="gm-gallery-arrow gm-gallery-next" data-step="1" aria-label="${label('label-next', 'Next picture')}">${ICON(NEXT)}</button>`;
            const large = zoom.querySelector('img');
            const go = (step) => {
                this.show(this.#index + step, false);
                this.#fillZoom();
            };
            zoom.addEventListener('click', (e) => {
                const arrow = e.target.closest('[data-step]');
                if (arrow)
                    go(Number(arrow.dataset.step));
                else if (e.target === large)
                    large.toggleAttribute('data-magnified');
                else
                    zoom.close();
            });
            zoom.addEventListener('pointermove', (e) => {
                const r = large.getBoundingClientRect();
                large.style.transformOrigin = `${((e.clientX - r.left) / r.width) * 100}% ${((e.clientY - r.top) / r.height) * 100}%`;
            });
            zoom.addEventListener('keydown', (e) => {
                const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
                if (step)
                    go(step);
            });
            zoom.addEventListener('close', () => this.#track?.focus());
            this.append(zoom);
            this.#zoom = zoom;
        }
        this.#fillZoom();
        this.#zoom.showModal();
    }
    #fillZoom() {
        const slide = this.slides[this.#index];
        const img = slide?.matches('img') ? slide : slide?.querySelector('img');
        const large = this.#zoom?.querySelector('img');
        if (!img || !large)
            return;
        large.src = img.currentSrc || img.src;
        large.alt = img.alt;
        large.removeAttribute('data-magnified');
        const [previous, next] = this.#zoom.querySelectorAll('[data-step]');
        previous.disabled = this.#index <= 0;
        next.disabled = this.#index >= this.slides.length - 1;
    }
}
define('gm-gallery', GmGallery);
//# sourceMappingURL=gallery.js.map