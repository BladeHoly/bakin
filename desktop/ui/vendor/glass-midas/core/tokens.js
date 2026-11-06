/** Design tokens. Every token is exposed as a CSS custom property `--gm-<group>-<name>`. */
export const tokens = {
    blur: { sm: '8px', md: '16px', lg: '28px' },
    /** Glass height in px for the refract tier; thicker glass bends more. */
    thickness: { thin: '12px', md: '24px', thick: '40px' },
    saturation: { md: '160%', lg: '200%' },
    tint: {
        light: 'rgb(255 255 255 / 0.18)',
        dark: 'rgb(20 20 28 / 0.32)',
        /** Opaque enough to read text on with no backdrop effect, for the solid tier. */
        solid: 'rgb(28 28 36 / 0.88)',
    },
    highlight: { edge: 'rgb(255 255 255 / 0.55)', glow: 'rgb(255 255 255 / 0.12)' },
    radius: { sm: '10px', md: '18px', lg: '28px', pill: '999px' },
    /** Gaps and padding, one scale for every layout. */
    space: {
        '2xs': '2px',
        xs: '4px',
        sm: '8px',
        md: '12px',
        lg: '16px',
        xl: '24px',
        '2xl': '32px',
        '3xl': '48px',
        '4xl': '64px',
    },
    /** Font sizes, from captions to display headings. */
    text: {
        xs: '0.75rem',
        sm: '0.875rem',
        md: '1rem',
        lg: '1.125rem',
        xl: '1.375rem',
        '2xl': '1.75rem',
        '3xl': '2.25rem',
        '4xl': '3rem',
    },
    /** Colors that mean something: never decoration. */
    status: {
        info: 'rgb(120 170 255)',
        success: 'rgb(90 210 150)',
        warning: 'rgb(255 196 90)',
        danger: 'rgb(255 110 110)',
    },
};
export function tokenVar(group, name) {
    return `var(--gm-${group}-${name})`;
}
/** Serializes the tokens as a CSS rule, ready to inject or write to a stylesheet. */
export function tokensToCss(selector = ':root', source = tokens) {
    const lines = [];
    for (const [group, values] of Object.entries(source)) {
        for (const [name, value] of Object.entries(values)) {
            lines.push(`  --gm-${group}-${name}: ${value};`);
        }
    }
    return `${selector} {\n${lines.join('\n')}\n}`;
}
//# sourceMappingURL=tokens.js.map