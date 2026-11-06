import { displacementMap } from './displacement.js';
import { detectTier } from './support.js';
const SVG_NS = 'http://www.w3.org/2000/svg';
let filterCount = 0;
// Maps already drawn, shared by every surface: a size or shape seen before (a nav that
// expands and collapses, identical cards) costs nothing the second time.
const CACHE_SIZE = 32;
const maps = new Map();
// While a surface keeps changing size (an animation, a window drag), its last map is
// stretched to fit and the exact one is drawn once the size holds this long.
const SETTLE_MS = 140;
// Stretching works for small moves; past this much (either side) the map is not stretched.
const STRETCH_LIMIT = 0.15;
function mapFor(width, height, optics) {
    const density = Math.min(devicePixelRatio || 1, 2);
    const key = JSON.stringify([width, height, optics, density]);
    let entry = maps.get(key);
    if (entry) {
        maps.delete(key); // refresh its place in the LRU order
    }
    else {
        const map = displacementMap(width, height, optics, density);
        const canvas = document.createElement('canvas');
        const ctx = map && canvas.getContext('2d');
        if (!map || !ctx)
            return undefined;
        canvas.width = map.width;
        canvas.height = map.height;
        const image = ctx.createImageData(map.width, map.height);
        image.data.set(map.data);
        ctx.putImageData(image, 0, 0);
        entry = { url: canvas.toDataURL(), scale: map.scale };
        if (maps.size >= CACHE_SIZE)
            maps.delete(maps.keys().next().value);
    }
    maps.set(key, entry);
    return entry;
}
function setBackdrop(el, value) {
    el.style.setProperty('backdrop-filter', value);
    el.style.setProperty('-webkit-backdrop-filter', value);
}
/** Turns an element into a glass surface, picking the best tier the browser can render. */
export function createGlass(el, initial = {}) {
    let options = initial;
    const tier = options.tier ?? detectTier();
    const id = `gm-glass-${++filterCount}`;
    let svg;
    let observer;
    let drawn = ''; // optics and size of the map in the filter
    let shape = ''; // the same without the size
    let drawnSize = [0, 0];
    let settle;
    el.dataset.glassTier = tier;
    const render = (resizing = false) => {
        if (tier === 'solid')
            return setBackdrop(el, 'none');
        if (tier === 'blur') {
            const blur = options.blur ?? 'var(--gm-blur-md, 16px)';
            return setBackdrop(el, `blur(${blur}) saturate(var(--gm-saturation-md, 160%))`);
        }
        // Refraction carries the look, so the frost stays light.
        const tail = `blur(${options.blur ?? '1px'})`;
        const width = el.offsetWidth;
        const height = el.offsetHeight;
        // Defaults: a crisp rim, a thin squircle bezel over 40 px of glass that folds the backdrop.
        const optics = {
            radius: options.radius ?? 18,
            bezel: options.bezel ?? 20,
            thickness: options.thickness ?? 40,
            ior: options.ior ?? 1.5,
            curvature: options.curvature ?? 4,
            magnify: options.magnify ?? 0,
            caustics: options.caustics ?? true,
            lightAngle: options.lightAngle ?? -60,
        };
        const specular = options.specular ?? 0.2;
        const saturation = options.specularSaturation ?? 4;
        const chroma = Math.min(Math.max(options.chroma ?? 0, 0), 0.5);
        const nextShape = JSON.stringify([optics, specular, saturation, chroma]);
        const next = JSON.stringify([width, height, nextShape]);
        if (next === drawn)
            return setBackdrop(el, `${tail} url(#${id})`);
        clearTimeout(settle);
        if (resizing && svg && nextShape === shape) {
            // Only the size moved: stretch the current map now, draw the exact one when it settles.
            // A big change of shape (a sidebar folding, a window maximized) would bend a stretched
            // rim into arcs, so until then that glass is only frosted.
            const [w0, h0] = drawnSize;
            const reshaped = Math.abs(width / w0 - 1) > STRETCH_LIMIT || Math.abs(height / h0 - 1) > STRETCH_LIMIT;
            if (reshaped) {
                setBackdrop(el, `blur(${options.blur ?? 'var(--gm-blur-md, 16px)'})`);
            }
            else {
                for (const node of svg.querySelectorAll('filter, feImage')) {
                    node.setAttribute('width', `${width}`);
                    node.setAttribute('height', `${height}`);
                }
            }
            settle = setTimeout(() => render(), SETTLE_MS);
            return;
        }
        const map = mapFor(width, height, optics);
        if (!map)
            return setBackdrop(el, tail);
        if (!svg) {
            svg = document.body.appendChild(document.createElementNS(SVG_NS, 'svg'));
            svg.setAttribute('aria-hidden', 'true');
            svg.style.cssText = 'position:absolute;width:0;height:0;pointer-events:none';
        }
        // Bend the backdrop (smoothing magnified pixels unless the rim folds on purpose), then
        // light the rim: a saturated copy of the bent backdrop masked by the hairline in the blue
        // channel, plus the hairline itself in white at the specular opacity.
        const displace = (scale, result) => `<feDisplacementMap in="SourceGraphic" in2="map" scale="${scale}" xChannelSelector="R" yChannelSelector="G" result="${result}"/>`;
        // Dispersion: red bends 1 − chroma times as far and blue 1 + chroma, each pass keeping one
        // channel, summed back. Where the glass is flat the passes agree, so only the rim splits.
        const only = (c) => [0, 1, 2].map((row) => [0, 1, 2, 3, 4].map((col) => +(row === c && col === c))).join(' ') +
            ' 0 0 0 1 0';
        const channel = (c, scale) => displace(scale, `d${c}`) +
            `<feColorMatrix in="d${c}" values="${only(c).replace(/,/g, ' ')}" result="c${c}"/>`;
        const bend = chroma
            ? channel(0, map.scale * (1 - chroma)) +
                channel(1, map.scale) +
                channel(2, map.scale * (1 + chroma)) +
                `<feComposite in="c0" in2="c1" operator="arithmetic" k2="1" k3="1" result="c01"/>` +
                `<feComposite in="c01" in2="c2" operator="arithmetic" k2="1" k3="1" result="bent"/>`
            : displace(map.scale, 'bent');
        const smooth = optics.caustics
            ? ''
            : '<feGaussianBlur in="bent" stdDeviation="0.6" result="bent"/>';
        svg.innerHTML =
            `<filter id="${id}" x="0" y="0" width="${width}" height="${height}" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">` +
                `<feImage href="${map.url}" width="${width}" height="${height}" preserveAspectRatio="none" result="map"/>` +
                bend +
                smooth +
                `<feColorMatrix in="map" values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 1 0 0" result="rim"/>` +
                `<feColorMatrix in="bent" type="saturate" values="${saturation}" result="vivid"/>` +
                `<feComposite in="vivid" in2="rim" operator="in" result="vividRim"/>` +
                `<feComposite in="vividRim" in2="bent" result="lit"/>` +
                `<feComponentTransfer in="rim" result="line"><feFuncA type="linear" slope="${specular}"/></feComponentTransfer>` +
                `<feComposite in="line" in2="lit"/>` +
                `</filter>`;
        drawn = next;
        drawnSize = [width, height];
        shape = nextShape;
        // Frost first, then bend: the rim light added by the filter stays crisp.
        setBackdrop(el, `${tail} url(#${id})`);
    };
    render();
    if (tier === 'refract' && typeof ResizeObserver !== 'undefined') {
        observer = new ResizeObserver(() => render(true));
        observer.observe(el);
    }
    return {
        tier,
        update(next = {}) {
            options = { ...options, ...next };
            render();
        },
        destroy() {
            clearTimeout(settle);
            observer?.disconnect();
            svg?.remove();
            setBackdrop(el, '');
            delete el.dataset.glassTier;
        },
    };
}
//# sourceMappingURL=glass.js.map