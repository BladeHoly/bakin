import { Base, define } from './base.js';
const SIZE = /^([\d.]+)\s*(b|kb|mb|gb)?$/i;
const UNITS = { b: 1, kb: 1024, mb: 1024 ** 2, gb: 1024 ** 3 };
/** "5 MB" or "500kb" or a plain number of bytes, as bytes; Infinity if unset or unreadable. */
export const bytes = (s) => {
    const m = s?.trim().match(SIZE);
    return m ? Number(m[1]) * UNITS[(m[2] ?? 'b').toLowerCase()] : Infinity;
};
const readable = (n) => n < 1024
    ? `${n} B`
    : n < 1024 ** 2
        ? `${Math.round(n / 1024)} KB`
        : `${Number((n / 1024 ** 2).toFixed(1))} MB`;
/** Whether a file is one an `accept` list allows: ".png", "image/*", "image/png". */
export const accepts = (accept, file) => !accept.trim() ||
    accept.split(',').some((raw) => {
        const a = raw.trim().toLowerCase();
        if (a.startsWith('.'))
            return file.name.toLowerCase().endsWith(a);
        if (a.endsWith('/*'))
            return file.type.startsWith(a.slice(0, -1));
        return file.type === a;
    });
/**
 * `<gm-upload max-size="5 MB" max-files="6"><input type="file" accept="image/*" multiple>
 * </gm-upload>`: a place to drop files or choose them, and the list of the chosen ones, each
 * with a preview (for pictures), its size and a button to take it off.
 *
 * The input stays the source of truth: its `files` always hold the list, so the form sends them.
 * Choosing more adds to the list (with `multiple`) instead of replacing it. A file that doesn't
 * fit `accept`, `max-size` or `max-files` is left out and reported by `gm-reject` with a
 * `reason`; every change fires `gm-change` with the `files`. Anything else inside is the drop
 * zone's text; without it, "Drop files here, or choose them".
 */
export class GmUpload extends Base {
    #input = null;
    #list;
    #files = [];
    #urls = [];
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
        this.#revoke();
    }
    #build() {
        const input = this.querySelector(':scope > input[type="file"]');
        if (!input || this.#list)
            return;
        this.#input = input;
        const zone = document.createElement('label');
        zone.className = 'gm-upload-zone';
        const text = [...this.childNodes].filter((n) => n !== input);
        zone.append(input);
        if (text.some((n) => n.textContent?.trim()))
            zone.append(...text);
        else {
            const p = document.createElement('span');
            p.innerHTML = `${this.getAttribute('label') ?? 'Drop files here, or <u>choose them</u>'}`;
            zone.append(p);
            text.forEach((n) => n.remove());
        }
        const hint = this.#hint();
        if (hint) {
            const small = document.createElement('small');
            small.textContent = hint;
            zone.append(small);
        }
        const list = document.createElement('ul');
        list.className = 'gm-upload-list';
        list.addEventListener('click', (e) => {
            const b = e.target.closest('[data-remove]');
            if (!b)
                return;
            const at = Number(b.dataset.remove);
            this.files = this.#files.filter((_, k) => k !== at);
            // Keep focus nearby: on the next remove button, else on the zone's input.
            const next = list.querySelectorAll('[data-remove]')[Math.min(at, this.#files.length - 1)];
            (next ?? input).focus();
        });
        this.append(zone, list);
        this.#list = list;
        // Setting `files` from code fires no change, so this is always a person choosing: the
        // input holds just the new ones, which join the list.
        input.addEventListener('change', () => this.#add([...(input.files ?? [])], input.multiple));
        for (const type of ['dragenter', 'dragover']) {
            zone.addEventListener(type, (e) => {
                e.preventDefault();
                if (!input.disabled)
                    this.toggleAttribute('data-dragging', true);
            });
        }
        for (const type of ['dragleave', 'drop']) {
            zone.addEventListener(type, (e) => {
                e.preventDefault();
                this.removeAttribute('data-dragging');
                const files = e.dataTransfer?.files;
                if (type === 'drop' && files && !input.disabled)
                    this.#add([...files], input.multiple);
            });
        }
    }
    #hint() {
        const parts = [];
        const max = bytes(this.getAttribute('max-size'));
        if (Number.isFinite(max))
            parts.push(`Up to ${readable(max)} each`);
        const count = Number(this.getAttribute('max-files'));
        if (count)
            parts.push(`${count} at most`);
        return parts.join(', ');
    }
    #add(chosen, multiple) {
        const input = this.#input;
        if (!input)
            return;
        const max = bytes(this.getAttribute('max-size'));
        const limit = multiple ? Number(this.getAttribute('max-files')) || Infinity : 1;
        const next = multiple ? [...this.#files] : [];
        for (const file of chosen) {
            let reason;
            if (!accepts(input.accept, file))
                reason = 'type';
            else if (file.size > max)
                reason = 'size';
            else if (next.length >= limit)
                reason = 'count';
            if (reason) {
                this.dispatchEvent(new CustomEvent('gm-reject', {
                    detail: { file, reason },
                    bubbles: true,
                }));
            }
            else
                next.push(file);
        }
        this.files = next;
    }
    /** The chosen files. Setting it replaces the list (and the input's `files`). */
    get files() {
        return [...this.#files];
    }
    set files(files) {
        this.#files = [...files];
        const input = this.#input;
        if (input && typeof DataTransfer !== 'undefined') {
            try {
                const dt = new DataTransfer();
                this.#files.forEach((f) => dt.items.add(f));
                input.files = dt.files;
            }
            catch {
                // An engine without a constructible DataTransfer: the list still shows the files.
            }
        }
        this.#draw();
        this.dispatchEvent(new CustomEvent('gm-change', { detail: { files: this.files }, bubbles: true }));
    }
    #revoke() {
        this.#urls.forEach((u) => URL.revokeObjectURL?.(u));
        this.#urls = [];
    }
    #draw() {
        const list = this.#list;
        if (!list)
            return;
        this.#revoke();
        const remove = this.getAttribute('label-remove') ?? 'Remove';
        list.replaceChildren(...this.#files.map((file, k) => {
            const li = document.createElement('li');
            const preview = document.createElement('span');
            preview.className = 'gm-upload-preview';
            preview.setAttribute('aria-hidden', 'true');
            if (file.type.startsWith('image/') && URL.createObjectURL) {
                const url = URL.createObjectURL(file);
                this.#urls.push(url);
                const img = document.createElement('img');
                img.src = url;
                img.alt = '';
                preview.append(img);
            }
            else
                preview.textContent = file.name.split('.').pop()?.slice(0, 4) ?? '';
            const name = document.createElement('span');
            name.className = 'gm-upload-name';
            name.textContent = file.name;
            const size = document.createElement('small');
            size.textContent = readable(file.size);
            name.append(size);
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'gm-upload-remove';
            b.dataset.remove = String(k);
            b.setAttribute('aria-label', `${remove} ${file.name}`);
            b.innerHTML =
                '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
            li.append(preview, name, b);
            return li;
        }));
        list.hidden = this.#files.length === 0;
    }
}
define('gm-upload', GmUpload);
//# sourceMappingURL=upload.js.map