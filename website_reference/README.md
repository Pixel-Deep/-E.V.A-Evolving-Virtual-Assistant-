# EVA Website Design Reference

This local, self-contained reference is EVA's visual benchmark for polished, responsive websites. Open `index.html` directly in a browser. Its hero illustration is bundled at `assets/campus-illustration.svg`; it works offline and does not depend on a third-party image host. The page also includes responsive navigation, accessible focus states, restrained interactions, reduced-motion support, and a multi-section layout.

## Visual principles

- **Palette:** paper `#FBFAF6`, forest `#234F3D`, deeper forest `#173F35`, ink `#20332B`, muted text `#65736B`, terracotta `#B96545`, soft amber `#F2C48D`, sage `#E1EBE0`, peach `#F7E7DC`, and lilac `#ECE8F2`.
- **Typography:** expressive system serif for display headings; clean system sans-serif for body text. The design remains polished if external fonts or the network are unavailable.
- **Layout:** centered 1160px maximum content width; 28px desktop gutters and 20px mobile gutters; spacious two-column hero, balanced text widths, three feature cards, closing call to action, and a useful footer.
- **Type scale:** hero headline `clamp(3.4rem, 6vw, 5.4rem)` on desktop and responsive down to about `3.25rem` on small screens; section headings `clamp(2.7rem, 4.7vw, 4rem)`; body copy 16px with 1.65 line-height; supporting hero copy about 17px.
- **Spacing:** use an 8px rhythm (8, 16, 24, 32, 48, 64, 96px) with larger section spacing where the composition needs breathing room.
- **Components:** pill-shaped primary CTA, restrained borders, soft low-contrast shadows, 12–24px corner radii, purposeful image framing, and hover motion limited to small lifts/scales.
- **Accessibility:** semantic landmarks, descriptive image alt text, visible keyboard focus, responsive navigation, and `prefers-reduced-motion` support.

This is a design-quality reference, not a universal page template. EVA should adapt the visual direction to the user's audience and task; dashboards and data tools may need a denser layout, while portfolios and marketing pages may need more visual storytelling. Do not copy the sample school's name or claims. Do not add generic copyright/year/credit footers.
